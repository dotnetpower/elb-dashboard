# App Insights connection string survives full provision

## Motivation
The Telemetry connection string applied via Settings → "Apply to server
sidecars" kept disappearing after deploys. Root cause: `infra/main.bicep` wired
the sidecar `APPLICATIONINSIGHTS_CONNECTION_STRING` to
`monitoring.outputs.appInsightsConnectionString`, which is empty when
`enableApplicationInsights=false` (deployments pointing telemetry at an external
component). `quick-deploy.sh` preserves env (upsert), but a full `azd provision`
re-applied the empty Bicep value and wiped the operator's override.

## User-facing change
The connection string now persists across full provisions when set via
`azd env set APPLICATIONINSIGHTS_CONNECTION_STRING <value>`. The value is read
into a new param and used only when the deployment does not create its own
App Insights — no value is committed to the repo.

## API / IaC diff
- `infra/main.bicep`: new param `applicationInsightsConnectionStringOverride`;
  sidecar env + output use `empty(monitoring output) ? override : monitoring`.
- `infra/main.parameters.json`: maps the param to azd env
  `APPLICATIONINSIGHTS_CONNECTION_STRING` (empty default).
- `infra/main.json`: regenerated.

## Validation
- `az bicep build infra/main.bicep` — clean.
- `uv run pytest -q api/tests/test_settings_app_insights.py` — 12 passed.
- Customer env: value set in azd env + applied live to api/worker/beat.
