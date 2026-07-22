# App Insights connection string survives redeploys (durable store)

## Motivation

Operators reported that the Application Insights connection string applied in
Settings → Telemetry "keeps disappearing." Root cause: the applied connection
string only existed as the `APPLICATIONINSIGHTS_CONNECTION_STRING` env var on the
Container App revision. A full `azd provision` / `quick-deploy.sh all` re-applies
the Bicep template with `applicationInsightsConnectionString` sourced from the
azd env value (empty by default), which **wipes** the env var. Unlike the Service
Bus integration config (persisted in the `servicebuspref` Table), telemetry had
no durable store, so the override was lost on the next full deploy.

## User-facing change

- Applying a connection string ("Apply to server sidecars") now also persists it
  to a durable single-row Azure Table (`appinsightspref`).
- After a full redeploy wipes the env var, the connection string **self-heals**:
  - the Settings → Telemetry status endpoint reports the persisted value again;
  - backend OpenTelemetry export re-initialises from the persisted value on the
    next sidecar restart — **no extra revision swap**, because the fallback is a
    read, not a re-apply.
- "Clear server override" now also removes the persisted row, so a cleared
  override does not resurface.
- Browser-side behaviour is unchanged; the SPA response shape is identical.

## API / IaC diff summary

Backend only (no API contract change, no IaC change):

- New `api/services/app_insights_pref.py` — durable store (Table in Container
  Apps, JSON file locally), mirroring `service_bus_pref`. `get_*` never raises.
- `api/services/app_insights_provisioning.py` `deployment_connection_string()` —
  env var first, else fall back to the persisted row.
- `api/app/telemetry.py` — `init_telemetry` resolves via a new
  `_resolve_connection_string()` (env, then persisted fallback) so backend OTel
  self-heals.
- `api/tasks/azure/app_insights.py` — apply persists the value; clear removes it
  (both best-effort, never masking the deployment result).

## Validation evidence

- `uv run pytest -q api/tests/test_app_insights_pref.py api/tests/test_settings_app_insights.py` — 18 passed.
- `uv run pytest -q api/tests/test_telemetry_init.py api/tests/test_upgrade_aca_template.py api/tests/test_service_bus_telemetry.py` — 27 passed.
- `uv run pytest -q api/tests` — 4823 passed, 4 skipped.
- `uv run ruff check` on all touched files — clean.
