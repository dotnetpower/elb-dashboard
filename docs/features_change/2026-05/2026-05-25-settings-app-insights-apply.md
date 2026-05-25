# Settings App Insights Apply

## Motivation

Operators could create or paste an Application Insights connection string in Settings, but that only enabled browser-side telemetry. The deployed API, worker, and beat sidecars still kept an empty `APPLICATIONINSIGHTS_CONNECTION_STRING`, so server logs and traces did not flow to Application Insights.

## User-Facing Change

Enabling telemetry from Settings now applies a user-provided Application Insights connection string to the deployed server sidecars. Provisioning a new App Insights resource also applies the resulting connection string to the API, worker, and beat containers as part of the same task. The Settings panel includes an explicit Apply to server action for existing connection strings.

## API / IaC Diff Summary

- Added `POST /api/settings/app-insights/apply`, which enqueues a Celery task to update server-side telemetry configuration.
- Added a Container App template helper that patches `APPLICATIONINSIGHTS_CONNECTION_STRING` on `api`, `worker`, and `beat` and creates a new revision.
- Extended App Insights provisioning to apply the connection string to the deployed Container App when running in Azure.
- No Bicep shape change; this uses the existing Container App write permission granted to the control-plane identity.

## Validation Evidence

- Targeted backend tests: `uv run pytest -q api/tests/test_settings_app_insights.py api/tests/test_upgrade_aca_template.py`
- Frontend build: `cd web && npm run build`
