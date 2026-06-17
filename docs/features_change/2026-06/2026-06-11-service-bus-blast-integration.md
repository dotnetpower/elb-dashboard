# Optional Service Bus BLAST integration

## Motivation

External systems need a way to drive BLAST runs without calling the dashboard
or the sibling OpenAPI plane directly. A queue-backed ingestion point gives a
single, auditable, back-pressure-friendly path for every submission (dashboard
Run, OpenAPI `/v1/jobs`, and external producers), with completion signalled via
the durable job/result APIs and, when configured, optional topic events. The
actual result is fetched from the OpenAPI result endpoint (Claim-Check). The
feature is **optional and OFF by default**.

## User-facing change

- New **Settings → Service Bus** section: enable toggle, auth mode (Entra RBAC
  / SAS), namespace + request queue + optional completion topic configuration
  (with namespace discovery), a non-destructive connection test, live message
  counts, a dead-letter cleanup policy editor, and manual purge actions (behind
  a confirm dialog).
- When enabled, request messages on the queue are bridged to the sibling
  OpenAPI execution plane. Each job's status is available through the durable
  status/result APIs; deployments that configure a completion topic also publish
  one optional transition event per change, carrying a pointer to the OpenAPI
  result endpoint.

## API / IaC diff summary

- **New backend modules**: `api/services/service_bus_pref.py` (config row),
  `api/services/service_bus.py` (data-plane + admin wrapper — the only importer
  of `azure.servicebus`), `api/services/service_bus_tracking.py` (bridge rows),
  `api/tasks/servicebus/` (drain / publish-transitions / dlq-cleanup tasks +
  append-blob backup), `api/routes/settings/service_bus.py` (status / put /
  test / discover / purge).
- **New routes** under `/api/settings/service-bus` (all `require_caller`).
- **New beat schedules**: `servicebus-drain-and-resubmit`,
  `servicebus-publish-transitions`, `servicebus-dlq-cleanup` (all no-op unless
  enabled), routed to the `reconcile` queue.
- **New dependency**: `azure-servicebus==7.12.3`.
- **Infra**: `SERVICEBUS_ENABLED` (default `"false"`) added to
  `infra/control-plane-env.json` and wired into the api/worker/beat env arrays
  in `infra/modules/containerAppControl.bicep`.
- **quick-deploy.sh**: idempotent `ensure_service_bus_rbac` grants the shared
  MI `Azure Service Bus Data Sender`/`Data Receiver` on `SERVICEBUS_NAMESPACE`
  when exported (additive only; Entra/same-tenant namespaces).
- **Frontend**: `ServiceBusSection.tsx`, `settingsApi.*ServiceBus*` clients,
  `service-bus` wired into `SettingsPanel` + `useSettingsPanel`.
- **Docs**: `docs/architecture/service-bus-integration.md` (nav-wired).

## Persona impact (§12a)

No RBAC role narrowed. The quick-deploy grant is purely additive
(`phase` rule N/A). No new `Depends(require_caller)` on any SSE stream. The
`SERVICEBUS_ENABLED` guard ships default-OFF.

## Validation evidence

- `uv run ruff check api` — clean.
- `uv run pytest -q api/tests` — 3286 passed, 3 skipped (includes the 21 new
  Service Bus tests across `test_service_bus_pref.py`,
  `test_servicebus_tasks.py`, `test_settings_service_bus.py`, plus the existing
  `test_control_plane_env.py` and `test_pii_log_redaction.py` guards).
- `cd web && npm run build` — type-checks and builds.
- `uv run python scripts/docs/check_frontmatter.py` — OK (55 pages).
