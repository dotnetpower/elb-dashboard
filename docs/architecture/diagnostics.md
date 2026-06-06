---
title: Diagnose & Solve Problems — Reliability & Availability
description: Design of the read-only Reliability and Availability/Performance diagnostics that check the configured Azure resources against Well-Architected best practices and surface findings on a dedicated /diagnostics page.
social:
  cards_layout_options:
    title: Reliability & Availability Diagnostics
    description: Read-only best-practice checks for the configured AKS / Storage / ACR / Container App, surfaced as severity-ranked findings on a dedicated page.
tags:
  - architecture
  - operate
  - security
---

# Diagnose & Solve Problems — Reliability & Availability

> Status: design of record. The implementation lives under
> `api/services/diagnostics/` (engine + rule catalogs), `api/routes/diagnostics.py`
> (the `GET /api/diagnostics/{category}` surface), and
> `web/src/pages/diagnostics/` (the dedicated `/diagnostics` page).

## 1. Why

The Settings panel's **Diagnose & solve problems** section started as a narrow
slide-over with only **Identity and Security** implemented; **Reliability** and
**Availability and Performance** were `Coming soon` placeholders. The panel is
too small to render per-resource best-practice findings, and the monitor data
plane it would reuse **degrades open** (returns an empty payload on failure),
which would silently turn "I could not check" into a false "no problems found".

This design adds two real diagnostic categories that check the **configured
Azure resources** (the AKS cluster(s), the Storage account, the ACR registry,
the Container App, and the request/queue surface) against
[Azure Well-Architected Framework](https://learn.microsoft.com/azure/well-architected/)
**Reliability** and **Performance Efficiency** practices, and moves the whole
experience onto a **dedicated `/diagnostics` page** so each finding has room to
explain itself and link to the fix.

## 2. Non-negotiable constraints (charter)

- **Read-only.** Diagnostics never mutate Azure. No state machine, no side
  effects → no idempotency / concurrency hazards. Repairs are delegated to the
  surfaces that already own them (AKS card, Storage card, Settings sections).
- **Failure & permission-denied surface as findings, not silence.** Like
  `GET /api/me/access-review` (which deliberately does **not** degrade open), an
  un-fetchable resource yields an `indeterminate` finding with the reason, never
  a fabricated `ok`.
- **Reader persona must keep working.** A subscription Reader legitimately
  cannot see some data-plane / write scopes. A permission-denied is classified
  `indeterminate` ("cannot verify with this role"), **never `critical`**, so the
  Persona Matrix `reader_caller` does not regress.
- **By-design choices are not defects.** `minReplicas=1` and the ephemeral Redis
  broker are intentional cost decisions in the charter; rules tag these
  `expected_by_charter=true` and emit `info`, not `warning`/`critical`.
- **Auth + sanitisation.** Every `/api/diagnostics/*` route enforces
  `require_caller`. All finding text and resource references pass through
  `sanitise()` and are length-capped. No SAS tokens, no Storage
  `publicNetworkAccess` flip, no Azure Run Command (use the existing
  `monitoring` service `k8s_*` helpers).

## 3. Severity model

| Severity | Meaning | Example |
|---|---|---|
| `ok` | Checked, best practice met | AKS autoscaler enabled |
| `info` | By-design / informational | `minReplicas=1` (charter cost design) |
| `warning` | Best practice not met, not an outage | Storage LRS (single-region redundancy) |
| `critical` | Active or imminent reliability/availability risk | AKS not `Succeeded`, node memory pressure |
| `indeterminate` | Could not verify (permission / network / timeout) | Reader cannot read role assignments |

The page rolls findings up to a per-category chip set
(`critical N · warning M · indeterminate K · ok`) and groups them by resource
kind. `critical`/`warning` are expanded by default; `ok`/`info` collapse.

## 4. Backend

```
api/routes/diagnostics.py                 # GET /api/diagnostics/{category} (require_caller, sync)
api/services/diagnostics/
  __init__.py
  models.py        # Finding, DiagnosticReport, ResourceSnapshot (Pydantic v2 / dataclass)
  snapshot.py      # per-resource fetch with isolation → ResourceSnapshot(available, reason, data)
  engine.py        # gather snapshot (bounded, isolated) → evaluate rules → sanitise → report
  rules/
    __init__.py
    reliability.py   # pure (snapshot → list[Finding]) + thresholds + as_of
    availability.py
```

### 4.1 Data sources (already available, no new SDK surface)

| Resource | Reliability inputs | Availability inputs |
|---|---|---|
| AKS | `list_aks_clusters*` (provisioning/power state, agent pools, autoscale, k8s version, tags) | `k8s_node_request_pressure`, `k8s_top_nodes` (aggregated) |
| Storage | `get_storage_summary` (sku, public_network_access, is_hns) | `get_storage_summary` reachability |
| ACR | `list_acr_repositories` registry SKU | — |
| Container App | env (`CONTAINER_APP_*`), sidecar snapshot | `collect_snapshot` CPU/MEM/health |
| API / queue | — | `request_metrics.summarise` (p95/p99, error rate, RPM) |

### 4.2 Execution engine

- **One shared snapshot per request.** Fan-out is bounded by *resource kind*,
  not rule count: each resource is fetched at most once and injected into every
  rule that reads it.
- **Per-fetch isolation.** Each fetch is wrapped so one failure becomes an
  `unavailable(reason)` `ResourceSnapshot` for that resource only; sibling
  resources still produce findings (no `gather`-wide abort).
- **Bounded.** Concurrent fetches run under a `ThreadPoolExecutor` with a
  per-fetch timeout and an overall deadline; a fetch that overruns yields
  `indeterminate` ("timed out"), never an indefinite hang.
- **Cached.** The report is memoised via `cached_snapshot` (TTL ~30 s); the UI
  "Re-run" passes `fresh=true` to bypass it. The cache also absorbs
  double-click / multi-tab stampedes.
- **Permission classification.** A fetch that raises `AuthorizationFailed`/403
  marks the snapshot `access="denied"`; its rules emit `indeterminate`, never
  `critical` — this is what keeps the Reader persona green.

### 4.3 Rule catalog (versioned, pure-evaluable)

Rules are registered pure functions `(snapshot) -> Finding | None`, grouped in
`rules/reliability.py` and `rules/availability.py`. Thresholds live as module
constants; time-sensitive facts (k8s end-of-life cutoffs) carry an `as_of` date
and degrade to `info` ("verify version support") rather than asserting a stale
`critical`. Golden tests feed synthetic snapshots and assert the exact finding
set, so a threshold change touches only the catalog + golden fixture.

## 5. Frontend

```
web/src/pages/diagnostics/DiagnosticsPage.tsx   # /diagnostics(/:category) route
web/src/api/diagnostics.ts                       # typed client + Finding/Report types
web/src/mocks/diagnostics.ts                     # fixtures kept in lock-step with the contract
```

- The Settings **Diagnose & solve problems** card becomes a **launcher**:
  clicking a category closes the panel and `navigate('/diagnostics/:category')`.
  Identity and Security moves onto the same page (it graduates out of the narrow
  panel); browser history gives natural Back behaviour.
- The page is **on-demand** (one fetch on entry + an explicit **Re-run**); no
  TanStack auto-polling. An `AbortController` cancels in-flight diagnostics when
  the user navigates away or switches category.
- Severity is conveyed by **icon + colour + text** (never colour alone) for
  WCAG AA.
- Gated behind a `diagnostics` preview flag for staged rollout; default-on after
  stabilisation.

## 6. Edge cases

| Situation | Handling |
|---|---|
| No subscription configured | "Open the Setup Wizard first" (not `critical`) |
| AKS `power_state=Stopped` | `info` under Reliability (intended cost saving); `warning` under Availability ("stopped, cannot run work") |
| Storage `network_blocked` (private endpoint / local debug) | `indeterminate` ("data plane is private, expected"), not `critical` |
| Local dev (no `SHARED_IDENTITY_PRINCIPAL_ID`) | Identity card degrades gracefully (existing behaviour) |
| ARM throttling (429) | one bounded retry; then `indeterminate` |
| Reader cannot read a scope | `indeterminate` + a page banner: "N items could not be verified with your role" |

## 7. Rollout phases

1. **Phase 1** — backend engine + Reliability rules + golden tests (curl-verified, no UI).
2. **Phase 2** — `/diagnostics` page + Identity migration + Reliability rendering (behind the preview flag).
3. **Phase 3** — Availability/Performance rules + rendering.
4. **Phase 4** — e2e coverage, default the flag on, retire the in-panel detail.

## 8. Validation

- `uv run pytest -q api/tests/test_diagnostics_rules.py` (golden), plus
  `test_diagnostics_route.py` (contract / `require_caller` / sanitise / indeterminate).
- `uv run pytest -q api/tests/test_persona_matrix.py` (Reader non-regression).
- `cd web && npm run build` + the diagnostics e2e scenario.
- curl `GET /api/diagnostics/reliability` to inspect the finding schema and the
  permission-denied → `indeterminate` path.
