# Service Bus settings: copyable env-gate remediation commands

## Motivation

Enabling the Service Bus integration from **Settings → Service Bus integration**
only writes the deployment-wide config row (`enabled=true`). It does **not**, and
by design cannot, set the deployment master switch `SERVICEBUS_ENABLED`. That env
var is the second of two gates (`service_bus_enabled()` requires the env gate
**AND** the saved config — charter §12a Rule 4), and it lives on the Container App
revision (api / worker / beat sidecars), not in process-mutable state.

The recurring operator pain: after some redeploys the env gate resets to
`false`/absent, `effective_enabled` flips to `false`, and the Message Flow card
disappears. The fix is always the same one or two commands, but the operator had
to dig them out of docs/memory each time. The existing "not active yet" banner
explained *why* but did not hand over the exact command.

A true "Settings toggle auto-sets the env var" was deliberately **not** built: it
would require the backend to ARM-patch the Container App (new Container Apps write
RBAC on the shared MI — charter §12a Rule 1 two-phase), force a full control-plane
revision restart from a settings click, and still not survive the next
`azd` redeploy. The two-gate, deploy-time-env design is intentional.

## User-facing change

When the Service Bus config is `enabled` but the deployment master switch is OFF
(`env_gate_enabled === false`), the Settings remediation banner now renders two
copy-to-clipboard command blocks instead of prose-only guidance:

- **Durable (recommended):** `azd env set SERVICEBUS_ENABLED true && azd deploy`
  — survives every redeploy via the existing Bicep / quick-deploy azd-env wiring.
- **Fast (no redeploy):**
  `for c in api worker beat; do az containerapp update -n <control-plane-app> -g <control-plane-rg> --container-name $c --set-env-vars SERVICEBUS_ENABLED=true; done`
  — sets the gate on all three sidecars immediately; the integration goes live
  within ~1 minute.

Commands use generic placeholders (no deployment-specific identifiers). The copy
affordance mirrors the existing pattern in `PublicHttpsSection` and the VNet
peering `NsgRuleAction`. No behaviour change when the env gate is already ON.

## API / IaC diff summary

- Frontend only. No backend, route, env-default, Bicep, or RBAC change.
  - [web/src/components/settings/sections/ServiceBusSection.tsx](../../../web/src/components/settings/sections/ServiceBusSection.tsx):
    added a local `CopyCommand` helper and `Copy`/`Check` icons; enriched the
    `!envGateEnabled` branch of the "Enabled in settings, but not active yet"
    banner with the two copyable commands.
- The repo default `SERVICEBUS_ENABLED` in `infra/control-plane-env.json` stays
  `"false"` (charter §12a Rule 4); the two-gate model is unchanged.

## Validation evidence

- `cd web && npm run build` — ✓ built (type-check clean).
- `npx eslint src/components/settings/sections/ServiceBusSection.tsx` — exit 0.
- `get_errors` on the edited file — no errors.
- Backend `GET/PUT /api/settings/service-bus` contract unchanged; the SPA already
  consumed `env_gate_enabled` (`api/tests/test_settings_service_bus.py`
  `test_env_gate_reported_independently_of_config` still covers the field).
