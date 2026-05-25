# Server Application Insights Telemetry

## Motivation

Application Insights Investigate could show browser-side telemetry while the API, worker, and beat sidecars did not reliably appear as server telemetry. The server initializer existed, but application log export was disabled by default and only the API sidecar received the deployment connection string.

## User-facing change

When `APPLICATIONINSIGHTS_CONNECTION_STRING` is configured, the API now emits FastAPI request telemetry and `api.*` server logs to Application Insights with sidecar-specific cloud roles. Worker and beat sidecars also receive the same connection string so task logs can appear as server-side traces. Deployments without Application Insights configured continue to run with telemetry disabled as a no-op.

Celery task crashes, internal task errors, and revokes now also leave best-effort terminal entries in the matching `JobState` row and job history when the task can be associated with a `job_id` or `task_id`. This gives operators and users a dashboard-visible failure trail even when a task dies before its own domain-specific error handler runs.

The cluster create form also emits an info-level `cluster.provision.intent` browser log before preflight/provision calls. If a UI regression prevents the enqueue call, operators can distinguish "user clicked create" from "no action happened" by comparing this client intent event with the absence of a matching `/api/aks/provision` task. A periodic AKS provision reconciler marks stale queued/running `aks_provision` rows as failed when worker progress stops.

## API/IaC diff summary

- `api.app.telemetry.init_telemetry` now passes an OpenTelemetry `Resource` with `service.name`, `service.namespace`, and `service.instance.id`, limits log export to the `api` logger tree, and attaches FastAPI instrumentation directly to the app instance.
- FastAPI distro auto-instrumentation is disabled so request spans are attached once to the concrete app instance.
- `AZURE_MONITOR_DISABLE_LOGGING=true` remains an explicit opt-out for server log export by translating it to `OTEL_LOGS_EXPORTER=none` before Azure Monitor config is created.
- Worker telemetry initializes in Celery prefork child processes so task logs are exported by live child-local exporter threads.
- Celery `task_failure`, `task_internal_error`, and `task_revoked` signals update matching job state rows to `failed` or `cancelled` with a terminal event payload.
- The SPA logs `cluster.provision.intent` through `/api/client-log` before cluster preflight/provision, giving a server-side breadcrumb for click-without-enqueue UI failures.
- Celery beat runs `api.tasks.azure.reconcile_stale_aks_provisions` every five minutes to fail stale `aks_provision` rows whose `updated_at` stops advancing.
- `infra/modules/containerAppControl.bicep` injects `APPLICATIONINSIGHTS_CONNECTION_STRING` into the worker and beat sidecars in addition to the API sidecar.

## Validation evidence

- `uv run pytest -q api/tests/test_aks_stale_provision_reconciler.py api/tests/test_celery_failure_visibility.py api/tests/test_telemetry_init.py api/tests/test_settings_app_insights.py` — 16 passed.
- `uv run ruff check api/tasks/azure/provision.py api/tasks/azure/__init__.py api/celery_app.py api/tests/test_aks_stale_provision_reconciler.py api/tests/test_celery_failure_visibility.py api/app/telemetry.py api/main.py api/tests/test_telemetry_init.py` — passed.
- `cd web && npx tsc --noEmit` — passed.
- `az bicep build --file infra/main.bicep --outfile /tmp/elb-dashboard-main.json` — passed.