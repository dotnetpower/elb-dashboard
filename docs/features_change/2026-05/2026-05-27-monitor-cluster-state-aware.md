# App Insights cluster-aware monitor noise reduction (5-pillar plan)

## Motivation

Past 24 h of `appi-elb-dashboard` `exceptions` table showed 13 of 15 recorded
exceptions came from two structural problems in the monitor pipeline:

| Root cause | Count | Code path |
| --- | --- | --- |
| AKS cluster Stopped → per-poll connect-timeout to `<cluster>.hcp.koreacentral.azmk8s.io:443` | 7 | `monitor_cache._refresh` |
| Transient DNS `NameResolutionError` (Container Apps coredns hiccup) for both Stopped + Running clusters | 5 | `monitor_cache._refresh` |
| Wrapped `ConnectionError` / `HttpResponseError` from `monitor_cache._refresh` itself | 2 | same |
| `kubectl wait cert-manager-webhook` cold-cluster race (180 s timeout exhausted) | 1 | `api/tasks/openapi/public_https.py:_kubectl_or_raise` |

Both classes were *expected fallthroughs* in the dashboard's existing degrade-
gracefully design — the user never saw a 5xx because the routes return stale
or empty payloads — but each one was still recorded as a fresh App Insights
exception row with full stack on every poll tick, polluting the exception
stream and masking real bugs.

## User-facing change

* Stopped or deleted AKS clusters now render with a `Cluster stopped` /
  `Cluster missing` chip on the affected cards instead of a generic
  "Azure error" / no-data state, and stop generating per-poll backend
  exceptions. Healthy sibling clusters in the same subscription are
  unaffected — the gate is per `(subscription, RG, cluster)`.
* No change to who can do what; the gate is read-only ARM (`ManagedClusters.get`)
  under the existing shared user-assigned MI.

## API / IaC diff summary

### Backend (api/)

* New `api/services/cluster_health.py` — `get_cluster_health()` +
  `cached_snapshot_with_cluster_gate()`. Per-cluster ARM `power_state`
  cached for 90 s; degrades open when ARM is unreachable so a real K8s
  outage is still surfaced by the existing error path.
* New `api/tests/test_cluster_health.py` — 9 cases including
  multi-cluster isolation (stopped + running siblings).
* `api/routes/monitor/aks.py` — `/aks/nodes`, `/aks/pods`, `/aks/top-nodes`,
  `/aks/warmup-status` now use `cached_snapshot_with_cluster_gate` so a
  Stopped cluster short-circuits before any K8s API call.
* `api/services/monitor_cache.py`:
    * Classifies `requests.ConnectionError|ConnectTimeout|ReadTimeout`,
      `azure.core.exceptions.ResourceNotFoundError|ServiceRequestError`,
      and `HttpResponseError` with `status in {404,408,429,500,502,503,504}`
      as transient (`_is_transient_refresh_failure`).
    * `_should_suppress_transient_telemetry` dedups identical
      `(cache_key, exc_class)` failures in a 300 s sliding window —
      the first failure inside the window keeps `exc_info=True`, repeats
      log one-liners only (no AppInsights exception row).
    * Always increments OTel counter `elb_monitor_snapshot_refresh_failed`
      with `{exception_class, stale_fallback, transient}` attributes so
      operators can alert on sustained refresh failure rate independent
      of the exception stream.
    * Cold-miss or genuine programmer errors (`RuntimeError`, `ValueError`)
      still keep the full stack trace.
* `api/services/k8s/client.py`:
    * `_build_k8s_retry()` mounts a single fast urllib3 `Retry(total=1,
      connect=1, backoff_factor=0.5, status_forcelist=(),
      allowed_methods=GET|HEAD|OPTIONS)` on the pooled K8s session.
    * Absorbs the Container Apps env coredns NXDOMAIN hiccup that
      caused the "Running cluster, brief DNS failure" entries.
    * Env overrides: `K8S_SESSION_RETRY_TOTAL` (≤5), `K8S_SESSION_RETRY_BACKOFF` (≤5.0).
* `api/tasks/openapi/public_https.py`:
    * New `_wait_for_cert_manager_webhook()` helper — up to 5 short
      `kubectl rollout status` probes (60 s each, tolerates missing
      Deployment) then a single `kubectl wait --for=condition=Available
      --timeout=300s`. Replaces the old single-shot 180 s wait that
      raced the controller→cainjector→webhook creation order on cold
      clusters.

### Frontend (web/)

* `web/src/utils/monitorDegraded.ts` — added `cluster_stopped` /
  `cluster_not_found` to `DegradedReason` + REASON_TABLE descriptors
  so the existing diagnostics banner / card chips pick them up.
* `web/src/utils/monitorDegraded.test.ts` — 2 new pinning tests.

### IaC

None. No Bicep / Container App template / sidecar layout change.

## Validation evidence

* `uv run pytest -q api/tests/test_monitor_cache.py api/tests/test_cluster_health.py
  api/tests/test_k8s_retry.py api/tests/test_openapi_public_https.py` → all
  green (15 + 9 + 4 + 15 = 43 tests, all new code covered).
* `uv run pytest -q api/tests/test_smoke.py api/tests/test_monitor_graceful.py
  api/tests/test_response_contracts.py api/tests/test_route_contracts.py
  api/tests/test_auto_warmup.py api/tests/test_blast_tasks.py
  api/tests/test_k8s_warmup_status_parallel.py api/tests/test_monitoring_aks_subwide.py`
  → no regression in routes/monitor/auto-warmup/k8s suites (covered in
  the final review run).
* `cd web && npx vitest run src/utils` → all 44 utils tests pass.

## Out of scope (intentional)

* Auto-start, auto-delete, or automatic removal of stopped clusters — the
  dashboard surfaces state but never mutates a user's ARM resources without
  an explicit action.
* `WarmupPreference` row auto-pruning when the underlying cluster vanishes —
  the orphan badge in a future PR will let the user decide.
* OTel meter export to App Insights metrics namespace — the counter is
  declared but follows whatever export pipeline `init_telemetry` configures
  (no new exporter introduced here).
