# K8s API circuit breaker — stop the unreachable-cluster exception flood

## Motivation

A production App Insights sweep (Log Analytics workspace `log-elb-dashboard`)
found ~2000 `AppExceptions` per day, almost all
`requests.exceptions.ConnectionError` (`NameResolutionError`) against a single
stopped/deleted cluster's API server FQDN:

| Failing dependency | 2-day count |
| --- | --- |
| `GET /api/v1/namespaces/default/pods` | 2235 |
| `GET /api/v1/namespaces/default/services/elb-openapi` | 1356 |
| `ManagedClustersOperations.list_cluster_user_credentials` | 563 |

The dashboard keeps polling the cluster's Kubernetes API (and the ARM kubeconfig
fetch) on every monitor / external-job-sync tick. Once the cluster is stopped or
deleted the FQDN no longer resolves, so each poll throws a `ConnectionError`
that the OpenTelemetry `requests` instrumentor records as an App Insights
exception — even though our helpers already catch it and degrade gracefully. The
existing single connect-retry only smooths a transient coredns blip, not a
cluster that is down for minutes or hours.

(The same sweep confirmed **no** authorization / RBAC problems: zero `403` /
`AuthorizationFailed` in two days of traffic, and every HTTP 4xx/5xx request was
an expected graceful path — SPA 404s, sibling-webhook 401/503, expired-session
401, validation 422.)

## User-facing change

None visible in the UI. The dashboard already degraded gracefully; this removes
the telemetry noise and the wasted dependency calls that buried real errors.

### Behaviour

A per-cluster **circuit breaker** keyed by `(subscription, resource_group,
cluster)` now guards the single choke point every Kubernetes call funnels
through (`api/services/k8s/client.py::_get_k8s_session` + the ARM kubeconfig
fetch):

- After 2 consecutive connect/DNS failures (each already urllib3-retried once)
  the breaker **opens** for a 120 s cooldown.
- While open, `_get_k8s_session` / the credential fetch raise
  `ClusterApiUnreachable` (a builtin `ConnectionError` subclass, caught by the
  existing broad `except Exception` graceful handlers) **before** issuing any
  ARM or HTTP request — so the OTel instrumentor records nothing.
- The first successful answer (any HTTP status — a 4xx/5xx still proves the API
  server is reachable) closes the breaker; after the cooldown it optimistically
  closes and re-probes.
- One `LOGGER.info` line is emitted per trip (not per poll) so a down cluster
  stays visible without the flood.

Net effect: a long-down cluster records ~1 exception per 120 s cooldown instead
of one per poll — a >95% reduction — and the breaker self-heals the instant the
cluster comes back.

### Env knobs (all optional, sensible defaults)

- `K8S_CLUSTER_BREAKER_THRESHOLD` (default 2)
- `K8S_CLUSTER_BREAKER_COOLDOWN_SECONDS` (default 120)
- `K8S_CLUSTER_BREAKER_DISABLED` (default off — set truthy to fully bypass)

## API / IaC diff summary

Backend-only, no API/IaC change.

- `api/services/k8s/cluster_breaker.py` — new focused module: breaker state,
  `cluster_breaker_check/record_failure/record_success`, `reset_cluster_breaker`,
  and the `ClusterApiUnreachable` exception.
- `api/services/k8s/client.py` — breaker check at the top of `_get_k8s_session`
  (skips the pooled fast path too), ARM-failure recording in
  `_get_k8s_credential_material`, and a `session.request` wrapper
  (`_install_cluster_breaker`) that records HTTP connect success/failure. The
  test-only reset hooks now also clear the breaker.
- `api/tests/test_cluster_breaker.py` — 8 unit tests (trip threshold, raise
  while open, ConnectionError subclassing, success/cooldown close, disable flag,
  env override, and the `_get_k8s_session` short-circuit-before-ARM integration).

## Validation evidence

- `uv run pytest -q -n 0 api/tests/test_cluster_breaker.py` → 8 passed.
- `uv run pytest -q api/tests` → 3911 passed, 3 skipped (full backend sweep, no
  regressions).
- `uv run ruff check` on the new/modified files → clean.
- Source of the finding: Log Analytics KQL on workspace `log-elb-dashboard`
  (`AppExceptions` / `AppDependencies | where Success==false`).
