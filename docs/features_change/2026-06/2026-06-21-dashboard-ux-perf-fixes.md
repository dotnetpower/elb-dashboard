---
title: Dashboard UX + perf fixes — skeleton shimmer, fast session-expiry, HTTP inspector, K8s job declutter
description: Fix the invisible Jobs loading skeleton, cut expired-session detection from ~10s to ~2.5s, widen the HTTP inspector buffer, and declutter 2296 lingering K8s BLAST jobs slowing the monitor endpoints.
tags:
  - operate
  - ui
  - blast
---

# Dashboard UX + perf fixes: skeleton shimmer, fast session-expiry, HTTP inspector, K8s job declutter

## Motivation
Several operator-reported issues on the live dashboard: data tiles failing to
load / timing out (HTTP request inspector "Request timed out", cluster
**Workloads … Loading** stuck), the BLAST Jobs loading skeleton looking static,
an expired login session taking ~10 s before redirecting to sign-in, and the
HTTP request inspector table not reflecting all the requests shown in the
latency scatter. Plus a root-cause request for failed BLAST runs.

## Root causes (each confirmed, not guessed)

| # | Symptom | Root cause |
|---|---------|------------|
| C | Jobs loading skeleton "not animated" | `.skeleton` shimmer ran, but its gradient stops reused `--bg-tertiary`/`--bg-hover` (`#242830` vs `#282d36`, ~2% lightness apart) → the animation was imperceptible. Light theme was worse (highlight was *darker* than the base). |
| D | Expired session: ~10 s, then redirect | `getAccessToken` (client.ts) retried `acquireTokenSilent` **3×** with 1 s+2 s exponential backoff on *every* error class, and only fast-failed on the exact `err.name === "InteractionRequiredAuthError"` string. An expired session that threw a different MSAL error (e.g. `ServerError` for a dead refresh token) fell through to the full retry budget ≈ 10 s. |
| E (table) | Inspector response times "not all in the bottom list" | The latency **scatter** reads the 8192-entry metrics buffer; the per-request **table** reads a separate **256**-entry detail buffer and the panel asked for only 200 rows → the table aged out requests still visible in the scatter. |
| A | Data not loading / inspector "Request timed out" / Workloads stuck | The api sidecar lists Kubernetes pods/jobs on the monitor/cluster endpoints, and the cluster had accumulated **2296 terminal BLAST Jobs + 948 pods** (no `ttlSecondsAfterFinished` on the job template) → every K8s list was slow + heavy, saturating the single-replica api under polling and tripping client timeouts. |
| B | Failed BLAST runs | The failures were the `-negative_taxids`/`-taxids` sharded-`core_nt` bug (missing `.nos`/`.not` taxonomy filter index) — already root-caused, fixed and deployed (`elb-openapi 4.26`) with live Cowpox-virus parity. Diagnosability gap: `metadata/FAILURE.txt` is an 8-byte `FAILURE` marker (finalizer), not the blastn stderr, so the dashboard cannot explain *why*. |

## User-facing change / fixes shipped
* **C** — `web/src/theme/glass.css`: dedicated `--skeleton-base` / `--skeleton-highlight`
  tokens per theme with a perceptible (but still muted) lightness delta; `.skeleton`
  now uses them. The shimmer is visible again; `prefers-reduced-motion` still disables it.
* **D** — `web/src/api/client.ts`: classify `InteractionRequiredAuthError` robustly
  (`instanceof`, not a name string) and fast-fail it; cap the non-interaction path at
  one 500 ms retry (was 3× exponential), so expired-session detection drops from ~10 s
  to ~2.5 s. Auth is unchanged — the backend still validates every token; only the
  *failure-detection speed* changed.
* **E (table)** — `api/services/request_metrics.py` raises the detail buffer 256→512;
  `HttpInspectorPanel.tsx` raises the panel limit 200→500 so the table tracks the
  scatter's window. The inspector now also keeps the **last successful snapshot**
  on a transient refresh failure (banner instead of blanking the table).
* **A** — one-time cleanup of the lingering terminal BLAST Jobs/pods
  (**2296 → 10 Jobs, 948 → 11 pods**, incl. three finalizer pods stuck Running for 12–13 h),
  which restores fast K8s list responses behind the monitor/cluster endpoints.
* **B** — confirmed the failure root cause (taxid `.nos`/`.not`, fixed in 4.26) and
  removed the 60 lingering failed Jobs in the cleanup above.

## Follow-ups (scoped, not in this change)
1. **Prevent K8s job re-accumulation** (durable fix for A/B clutter): either add
   `ttlSecondsAfterFinished` to the BLAST job template (needs an elb-openapi image
   rebuild) **or** a dashboard-side beat reconciler that GCs terminal `app=blast`
   Jobs older than N hours (ships with the api image, no openapi rebuild). Decision
   pending.
2. **Failure diagnosability** (B): make the runner's blastn-stderr upload land at a
   path the finalizer's `FAILURE` marker cannot clobber, so the dashboard can surface
   the real reason. Needs an elb-openapi/terminal rebuild.

## Validation evidence
* Backend: `uv run ruff check api` clean; `uv run pytest -q api/tests` → 4134 passed.
* Frontend: `cd web && npx vitest run` → 924 passed (101 files); `npm run build` clean.
* Self-critique caught + reverted a redundant route-level `/api/me/permissions` cache
  (the service layer `me_permissions.py` already caches with the same `(oid, scope)`
  key + 60 s TTL).
* Live cleanup verified on the dev cluster: `kubectl get jobs/pods -n default` 2296/948 → 10/11.

## Security
No security configuration changed: no auth/RBAC/network/JWT/CORS/ticket edits. The
session-expiry change only speeds failure detection (token validation unchanged); the
inspector buffer + skeleton are presentation; the job cleanup deletes terminal
workloads only.
