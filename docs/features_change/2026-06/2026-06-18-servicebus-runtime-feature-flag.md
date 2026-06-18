# Service Bus activation is a runtime feature flag (SERVICEBUS_ENABLED → 3-state override)

> Supersedes the same-day banner-only note
> [2026-06-18-servicebus-env-gate-remediation.md](2026-06-18-servicebus-env-gate-remediation.md):
> that change made the env-gate remediation copyable; this change removes the
> need for it in the common case by making the Settings toggle the activation
> switch.

## Motivation

`SERVICEBUS_ENABLED` was a hard deploy-time master switch: `service_bus_enabled()`
required the env var to be truthy **AND** the saved config to opt in. The env var
lives on the Container App revision, so any redeploy that did not carry the azd-env
pin reset it to the JSON default (`"false"`), `effective_enabled` flipped to
`false`, and the Message Flow card disappeared — a recurring operator surprise that
took a manual `az containerapp update --set-env-vars` to fix each time.

The deployment-wide config row (`servicebuspref` Table) is already the runtime
toggle: it survives redeploys and every sidecar reads it live. The only thing
stopping the Settings toggle from behaving like a real feature flag was the env
AND-gate.

## User-facing change

Enabling the integration in **Settings → Service Bus integration** (config
`enabled` + a namespace) now activates it directly — no env var, no redeploy, no
control-plane restart. It takes effect at the next gate check (~1 minute) across
all sidecars and survives redeploys (the config is Table-backed).

`SERVICEBUS_ENABLED` is redefined as a **three-state deploy-time override**:

| Env value | Behaviour |
|---|---|
| _empty / unset_ (new repo default) | **defer to the saved config** (runtime feature flag) |
| truthy (`true`/`1`/`yes`/`on`) | pin capability on — config still required |
| falsy (`false`/`0`/`no`/`off`) | **deployment kill switch** — force OFF regardless of config |

Default-OFF is preserved (charter §12a Rule 4): a fresh deployment has an empty
env **and** a disabled config, so it stays OFF until an authenticated operator
(non-Reader — `PUT /api/settings/service-bus` is not in the Reader allowlist)
opts in. A deployment that wants to forbid the integration sets
`SERVICEBUS_ENABLED=false` (kill switch).

The Settings "enabled but not active yet" banner now only fires for a kill switch
(with copyable commands to lift it) or a missing namespace — the old "deployment
gate OFF, redeploy to activate" case no longer exists.

## API / IaC diff summary

- `api/services/service_bus_pref.py`: new `service_bus_env_override()` (3-state)
  and `service_bus_kill_switch_on()`; `service_bus_enabled()` now = `not
  kill_switch AND cfg.enabled AND namespace`; `service_bus_env_gate_on()` keeps
  "explicitly pinned truthy" for diagnostics only.
- `api/routes/settings/service_bus.py`: status payload gains
  `kill_switch_enabled`.
- `web/src/api/settings.ts`: `ServiceBusStatusResponse.kill_switch_enabled`;
  `env_gate_enabled` doc clarified.
- `web/src/components/settings/sections/ServiceBusSection.tsx`: banner reworked
  to the kill-switch reason; state `envGateEnabled` → `killSwitchEnabled`.
- `infra/control-plane-env.json`: `SERVICEBUS_ENABLED` default `"false"` → `""`
  (defer) for api/worker/beat; `infra/modules/containerAppControl.bicep` param +
  comment updated; `infra/main.json` regenerated (`az bicep build`).
- No RBAC change, no new env guard, no Container App restart code path, no SAS.

## Validation evidence

- `uv run pytest -q` Service Bus + control-plane suites (9 files) — 132 passed.
  Rewrote `test_service_bus_enabled_three_state_override` +
  `test_service_bus_enabled_requires_config_even_when_env_truthy`
  (`api/tests/test_service_bus_pref.py`) and
  `test_env_override_three_state_in_status` +
  `test_get_defaults_disabled` (`api/tests/test_settings_service_bus.py`).
- `az bicep build --file infra/main.bicep --outfile infra/main.json` — clean;
  `infra/main.json` diff scoped (7/7) and `"SERVICEBUS_ENABLED": ""` inlined.
- `cd web && npm run build` + `eslint` on the edited section — clean.
