# Diagnose & solve problems — WAF/CAF catalog expansion (Reliability / Availability / Security)

## Motivation

The first diagnostics cut shipped ~12 checks across Reliability and Availability.
This expansion mines the Azure Well-Architected Framework (WAF) service guides
for AKS / Blob Storage / Container Registry and the Azure security baselines, and
applies **every recommendation that maps to a single fetchable configuration
field** on the resources this control plane actually provisions.

It deliberately does **not** fabricate checks to hit a round number: WAF items
that are design/process guidance (multi-region, chaos testing, capacity
planning, IaC) or that need a separate resource/call (backup vault, diagnostic
settings, Azure Policy evaluation, Key Vault) are out of scope for an automated
single-field probe and are intentionally omitted rather than rendered as a fake
`ok`/`warning`.

## User-facing change

- **New `Security posture` category** (WAF Security pillar) alongside the
  existing `Reliability` and `Availability and Performance`, on the dedicated
  `/diagnostics` page and as a Settings launcher card. Distinct from
  `Identity and Security` (which answers "what are MY roles") — this answers
  "is the resource hardened".
- **62 distinct checks** (up from ~12), each grounded in a Microsoft Learn WAF
  page, grouped by resource with severity rollup and recommendations:
  - **Reliability** — AKS SKU tier / uptime SLA, availability zones, system/user
    pool isolation, auto-upgrade + node-OS-upgrade channels, provisioning/power,
    autoscale, k8s version floor; Storage redundancy, blob/container soft delete,
    versioning, point-in-time restore, change feed; ACR SKU, zone redundancy,
    retention policy.
  - **Availability/Performance** — node request pressure (aggregated), Azure CNI
    vs kubenet, Standard vs Basic load balancer, Container Insights monitoring,
    sidecar health/headroom, API p95 latency + error rate.
  - **Security** — AKS Entra integration, Azure RBAC, local accounts disabled,
    private/IP-restricted API server, network policy, Azure Policy add-on,
    Defender, Workload ID, OIDC issuer, Key Vault CSI, managed identity,
    run-command disabled; Storage HTTPS-only, min TLS 1.2, shared-key disabled,
    anonymous blob access, OAuth default, cross-tenant replication, public
    network access (charter contract), firewall default-Deny, private endpoints,
    infrastructure encryption, CMK; ACR admin-user disabled, public network,
    anonymous pull, quarantine/trust policy, dedicated data endpoints, CMK.
- **Honest unknowns**: a field the SDK does not return (older API version, SKU
  that lacks the feature) makes the check **skip**, never fabricate. A
  permission denial / fetch failure stays `indeterminate` (Reader-safe).

## API / IaC diff summary

- **New service detail fetchers** (rich WAF/CAF surface, kept separate from the
  monitor card contract):
  `monitoring.serialise_cluster_detail` + `list_aks_clusters_detail_in_subscription`
  (AKS), `monitoring.get_storage_account_detail` (account props + blob-service
  props, best-effort second call), `monitoring.get_acr_registry_detail`.
- **New rule framework** `api/services/diagnostics/rules/specs.py` — declarative
  `RuleSpec` + `evaluate_specs` (skip-on-None, predicate-exception isolation) so
  the ~50 single-field checks are compact and golden-testable.
- **New catalog** `api/services/diagnostics/rules/security.py` (`evaluate_security`);
  `reliability.py` / `availability.py` extended with spec lists + custom
  multi-field checks (zones, pool isolation).
- **New category** `security` registered in the engine (reuses the reliability
  gatherer; cache key includes category) and added to the `DiagnosticCategory`
  literal + the SPA `diagnostics.ts` type and the `/diagnostics` rail.
- No IaC change. No new dependency. No SAS token, no Storage network flip, no
  Azure Run Command.

## Persona impact (§12a)

- Still read-only. Permission-denied → `indeterminate`, never `critical`; a
  subscription Reader sees "could not verify" per resource. `test_persona_matrix.py`
  unaffected (no scope narrowed, plain GET, no SSE change).

## Validation evidence

- Backend: `uv run pytest -q api/tests` → **2992 passed, 3 skipped**. New/extended
  golden tests: `test_diagnostics_rules.py` (reliability specs),
  `test_diagnostics_availability_rules.py` (perf-config specs),
  `test_diagnostics_security_rules.py` (security catalog + spec framework +
  predicate-exception isolation), `test_diagnostics_route.py` (detail-fetcher
  mocks), `test_diagnostics_snapshot.py`.
- Backend lint: `uv run ruff check api` → clean.
- Frontend: `npm run build` clean; `npx vitest run src/pages/diagnostics/` → 5 passed.
- Rule count: `grep -rhoE 'id="[a-z_]+\.[a-z_]+"' api/services/diagnostics/rules/ | sort -u | wc -l` → **62**.

## Hardening applied (critique loop)

- **Partial-failure isolation in the spec evaluator**: a predicate that raises on
  a malformed/unexpected field value is caught, logged at debug, and the spec is
  skipped — one bad field cannot abort the whole category (regression test added).
- **Sidecar-degraded → indeterminate** (carried from the prior cut): a
  Redis-unavailable all-`down` snapshot is `unavailable`, not a false `critical`.
- **Storage blob-service props are a best-effort second call**: failure leaves
  those fields `None` (checks skip), account-level checks still run.
