# App Insights init: enable Live Metrics by default

## Motivation
When an operator turns App Insights on (either through the deployment-injected
`APPLICATIONINSIGHTS_CONNECTION_STRING` or by provisioning a new resource from
the Settings panel), they expect the App Insights blade's **Live Metrics**
stream to start working out of the box. Until this change the api / worker /
beat sidecars initialised the Azure Monitor OpenTelemetry distro without the
`enable_live_metrics` kwarg, so QuickPulse stayed off and the Live Metrics
view in the portal showed "No data".

## User-facing change
- After the next revision rolls out, the App Insights → Live Metrics blade
  immediately streams per-second request / failure / dependency counters from
  the three Python sidecars whenever a connection string is present.
- No new UI controls. Existing on/off behaviour ("AI connection string set →
  telemetry on, unset → telemetry off") is unchanged.

## API / IaC diff summary
- [api/app/telemetry.py](../../../api/app/telemetry.py)
  `init_telemetry()` now passes
  `enable_live_metrics=True` to `configure_azure_monitor()` by default.
- New opt-out env var `AZURE_MONITOR_DISABLE_LIVE_METRICS=true` mirrors the
  existing `AZURE_MONITOR_DISABLE_LOGGING` switch. Default is **enabled** so
  no Bicep / Container App template changes are needed.
- No infra change; the kwarg is read by the SDK at process start.

## Validation
- `uv run pytest -q api/tests/test_telemetry_init.py` — 6 passed
  (new `test_init_honors_explicit_live_metrics_disable`).
- `uv run pytest -q api/tests` — 1622 passed.
- `uv run ruff check api/app/telemetry.py api/tests/test_telemetry_init.py` —
  clean.
- Deployment evidence will be captured on the next revision rollout by opening
  the App Insights resource → Live Metrics blade and confirming the three
  cloud-role nodes (`elb-api`, `elb-worker`, `elb-beat`) appear with non-zero
  request/heartbeat counters.
