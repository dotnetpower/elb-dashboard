# Feature lifecycle events to Application Insights

## Motivation

Operators asked where to look when a long-running operation (warmup, cluster
provisioning, BLAST database preparation, BLAST submit) silently fails. Phase
transitions were written only to the `jobstate` Azure Table and to ad-hoc
`LOGGER` lines, so there was no single, queryable "did this operation succeed or
fail, and why?" signal in Application Insights. We also wanted the behaviour to
be strictly conditional: when Application Insights is **not** configured, nothing
new should be emitted to Azure and there must be no added cost.

## User-facing change

The `worker` / `beat` sidecars now emit a structured **feature event** at the
terminal transition (`completed` / `failed` / `cancelled`) of four operation
families:

| `customEvents.name` | Task | Statuses |
| --- | --- | --- |
| `warmup` | `api.tasks.storage.warmup_database` | completed, failed |
| `cluster_provision` | `api.tasks.azure.provision` | completed, failed |
| `prepare_db` | `api.tasks.storage.prepare_db_via_aks` | completed, failed (partial) |
| `blast` | `api.tasks.blast.*` (submit / cancel / poll) | completed, failed, cancelled |

When `APPLICATIONINSIGHTS_CONNECTION_STRING` is set, each event lands in both the
`traces` and `customEvents` App Insights tables (via the
`microsoft.custom_event.name` attribute). When it is unset, the call degrades to
a local `api.events` log line with zero Azure ingestion — no production code path
forces telemetry on. Intermediate (non-terminal) phases continue to live in the
`jobstate` row + job history the dashboard renders live; only terminal
transitions are promoted to a named customEvent so the table is not flooded.

## API / IaC diff summary

- **New** `api/services/feature_events.py` — `record_feature_event(event, *, status, **attributes)`
  best-effort emitter on the `api.events` logger. Never raises; drops `None`
  attributes; sanitises string values; escapes reserved `LogRecord` keys; adds
  the `microsoft.custom_event.name` customEvent attribute. Exports
  `TERMINAL_STATUSES`.
- **Hooked** the four shared phase-update wrappers to emit on terminal status:
  `api/tasks/storage/warmup.py::_update_state`,
  `api/tasks/storage/prepare_db_via_aks.py::_update_state`,
  `api/tasks/azure/provision.py::_publish`,
  `api/tasks/blast/state.py::_update_state` (after the successful state write,
  not the no-op shortcut, so unchanged-state calls do not double-emit).
- **Docs** — new "Feature Lifecycle Events" section in
  `docs/user-guide/observability.md` with the event catalogue and KQL queries.
- No IaC change. No new dependency (uses the existing `azure-monitor-opentelemetry`
  pipeline and stdlib `logging`).

## Validation evidence

- `uv run pytest -q api/tests/test_feature_events.py` → 6 passed (no-raise,
  None-drop, sanitise, scalar pass-through, reserved-key escape, custom-event
  name).
- `uv run pytest -q api/tests/test_warmup_jobs.py api/tests/test_blast_tasks.py
  api/tests/test_azure_provision_aks.py api/tests/test_prepare_db_aks_task.py` →
  199 passed (hooks did not break existing task behaviour).
- `uv run python scripts/docs/check_frontmatter.py` → OK (54 navigated pages).
- `uv run ruff check api` → clean.
