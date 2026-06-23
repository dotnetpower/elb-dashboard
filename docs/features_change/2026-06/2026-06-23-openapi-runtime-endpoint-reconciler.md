# OpenAPI runtime endpoint re-stamp reconciler (Service Bus drain deadlock fix)

## Motivation

The Service Bus queue-drain readiness gate (`SERVICEBUS_QUEUE_AUTOSTART`) could
**deadlock the drain permanently** after a Container App revision restart:

* The IP-based OpenAPI runtime endpoint (`openapi:runtime:base-url`) lives in the
  ephemeral in-revision Redis, mirrored into the durable `dashboardsingletons`
  Storage Table. A freshness TTL (`OPENAPI_RUNTIME_ENDPOINT_MAX_AGE_SECONDS`,
  default 1 h) makes a cold read return `""` once the durable row ages past the
  window.
* Nothing re-stamps that row on a quiet deployment. So after a revision restart
  (Redis wiped) plus an idle hour, `external_blast._base_url` resolves nothing →
  `external_blast.ready` raises `openapi_not_configured` →
  `_openapi_ready_for_drain` returns False → the drain defers **forever**, even
  though the cluster is up.
* The only path that would refresh the endpoint is the drain itself — which the
  gate blocks. Chicken-and-egg.

The previous mitigation was pinning `ELB_OPENAPI_BASE_URL` by hand on the
worker/api containers. That pin survives `quick-deploy.sh` but is wiped by a full
`azd provision`, and hardcoding a cluster-specific IP in deploy config is brittle
(and the IP is environment-confidential, so it cannot live in the repo).

## User-facing change

None directly. Operationally, the Service Bus → OpenAPI drain keeps working after
a revision restart with **no manual `ELB_OPENAPI_BASE_URL` pin**: a new beat task
keeps the durable runtime endpoint inside the freshness window while the cluster
is reachable.

## API / IaC diff summary

* `api/tasks/openapi/reconcile_runtime_endpoint.py` (new): beat task
  `api.tasks.openapi.reconcile_runtime_endpoint`. No-op unless `SERVICEBUS_ENABLED`.
  Resolves the cluster context from the saved Service Bus config first, then the
  durable endpoint's own metadata (seeded by the `/api/blast/jobs` listing). Does
  one `k8s_get_service_ip` for the `elb-openapi` Service and, when it resolves
  (cluster up), re-stamps the durable endpoint via `save_openapi_base_url`
  (refreshing `updated_at`). When the IP does not resolve (cluster Stopped) it
  leaves the row to age out so the freshness gate still rejects an unreachable
  endpoint. Never raises.
* `api/tasks/openapi/__init__.py`: re-export the new task for Celery discovery.
* `api/celery_app.py`: `beat_schedule["openapi-runtime-endpoint-reconcile"]` every
  `CELERY_BEAT_OPENAPI_RUNTIME_ENDPOINT_SECONDS` (default 300 s — well inside the
  1 h freshness window).

No security guard is introduced (the task is inherently gated on the existing
`SERVICEBUS_ENABLED` feature flag), so no new default-OFF env / Bicep change is
needed. No managed-DB / SAS / Storage-network changes.

## Validation evidence

* `uv run pytest -q api/tests/test_openapi_runtime_endpoint_reconcile.py` → 5 passed
  (disabled no-op / no-cluster-context / re-stamp-from-SB-config /
  fallback-to-durable-metadata / Stopped-cluster-leaves-stale).
* `uv run pytest -q api/tests/test_openapi_public_https_reconcile.py
  api/tests/test_servicebus_tasks.py api/tests/test_openapi_runtime_endpoint_durable.py`
  → 78 passed (no regression in the sibling reconciler / drain gate / durable cache).
* `uv run ruff check api/tasks/openapi/reconcile_runtime_endpoint.py api/celery_app.py`
  → clean. Beat entry + Celery task registration asserted via a one-off import probe.
* Live: after deploy, the worker re-stamps the durable endpoint every 5 min while
  the cluster is up; the manual `ELB_OPENAPI_BASE_URL` pin is then removed and the
  drain verified to still resolve the endpoint (recorded with the customer deploy).
