---
title: Service Bus dashboard visibility — surface the deployment gate
description: Explain why a Settings-enabled Service Bus integration stays hidden on the dashboard (the deployment master switch SERVICEBUS_ENABLED is OFF), expose the env gate in the status API, warn in Settings, and let the production deploy pin the gate from a GitHub repo variable.
tags:
  - architecture
  - blast
---

# Service Bus dashboard visibility — surface the deployment gate

## Motivation

An operator enabled the Service Bus integration in **Settings → Service Bus**
but it never appeared on the dashboard. The dashboard's
`ServiceBusInboundStrip` (and the message-flow card) render only when
`effective_enabled` is true, and `effective_enabled = service_bus_enabled()`
requires **all three** of: the deployment master switch
`SERVICEBUS_ENABLED` (env gate), the saved config row `enabled`, and a
configured `namespace_fqdn`.

On the live deployment the env gate was `false` on every sidecar
(api/worker/beat), so the integration stayed dormant even though the runtime
config was enabled — and nothing in the UI explained why. The env gate is a
deployment-level opt-in (charter §12a Rule 4, default OFF) that the production
GitHub Actions deploy path reset to the `control-plane-env.json` default on
every redeploy, because the operator's `SERVICEBUS_ENABLED=true` pin lived only
in their local azd env (invisible to CI).

## User-facing change

- **Settings → Service Bus** now shows a clear warning banner when the config
  is enabled but the integration is not actually live, with the precise reason:
  deployment gate OFF (redeploy with `SERVICEBUS_ENABLED=true`), or no namespace
  configured yet.
- Once the deployment gate is ON and a namespace is configured, the dashboard
  inbound strip and message-flow card render as designed.

## API / IaC diff summary

- `api/services/service_bus_pref.py`: extracted `service_bus_env_gate_on()`
  (raw `SERVICEBUS_ENABLED` check) out of `service_bus_enabled()`; behaviour of
  the AND gate is unchanged.
- `api/routes/settings/service_bus.py`: `GET /api/settings/service-bus` now
  returns `env_gate_enabled` (the raw deployment master switch) alongside the
  existing `effective_enabled`, so the SPA can distinguish "deployment gate OFF"
  from "namespace missing".
- `web/src/api/settings.ts`: `ServiceBusStatusResponse` gains the
  `env_gate_enabled: boolean` field.
- `web/src/components/settings/sections/ServiceBusSection.tsx`: renders the
  warning banner; tracks `effective_enabled` / `env_gate_enabled` from the
  status response.
- `.github/workflows/deploy.yml`: passes `SERVICEBUS_ENABLED` from the GitHub
  repo variable into the deploy job env, so `quick-deploy.sh`'s
  `control_plane_env_pairs` pins the gate ON for every production deploy
  (unset/empty falls through to the repo default OFF, preserving the opt-in
  posture).

## Operational change (live deployment)

- GitHub repo variable `SERVICEBUS_ENABLED=true` was created so future
  production deploys keep the gate ON.
- The live Container App's `SERVICEBUS_ENABLED` was patched to `true` on
  api/worker/beat (new revision `ca-elb-dashboard--0000398`,
  `RunningAtMaxScale`) so the integration activates immediately without waiting
  for a redeploy.

Enabling the gate is safe for normal operation: `service_bus_enabled()` only
gates the read-only message-flow card and the inbound drain/publish/cleanup
beat tasks — it does **not** alter the dashboard's own BLAST submit path. The
DLQ cleanup task remains default-OFF.

## Validation

- `uv run pytest -q api/tests/test_settings_service_bus.py api/tests/test_service_bus_pref.py api/tests/test_message_flow.py` — 23 passed (includes a new
  `test_env_gate_reported_independently_of_config`).
- `uv run ruff check` on the touched backend files — clean.
- `cd web && npm run build` — clean; `tsc --noEmit` + eslint on the touched
  frontend files — clean.
- Live: `SERVICEBUS_ENABLED=true` confirmed on api/worker/beat; revision
  `0000398` `RunningAtMaxScale`; `/api/health` → 200.
