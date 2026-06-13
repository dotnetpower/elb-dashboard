# Service Bus env override survives redeploys + openapi LB-RBAC auto-detect & SPA fix

## Motivation

Two follow-ups after the openapi LB subnet-RBAC root-cause fix (issue #33):

1. The optional Service Bus integration env gate (`SERVICEBUS_ENABLED`) lives in
   `infra/control-plane-env.json` (default OFF) and was re-applied as `false` on
   every redeploy (both `quick-deploy.sh` and a full `azd provision`), so a
   deployment that turned Service Bus on lost it on the next deploy.
2. Issue #33 left two items open: classify the LB-pending cause as a distinct
   `lb_subnet_rbac_missing` state, and give the SPA a one-click grant button.

## User-facing change

- **Service Bus stays on across redeploys** when the deployment opts in. The
  repo default stays OFF (charter §12a Rule 4); a deployment pins it by setting
  `SERVICEBUS_ENABLED=true` in its azd env, which now overrides the JSON default
  on both deploy paths.
- **API page**: when the `elb-openapi` internal LoadBalancer is stuck `<pending>`
  because the cluster identity lacks Network Contributor on its node subnet, the
  spec panel now renders a **"Grant LB subnet RBAC"** button (instead of the
  generic "Repair VNet peering") that performs the idempotent grant.

## API / IaC diff summary

### A — per-deployment env override (default-preserving)
- `scripts/dev/quick-deploy.sh` `control_plane_env_pairs`: a control-plane key
  that is ALSO set in the process env (e.g. exported from azd env) wins over the
  JSON default. Set-vs-unset is explicit (`k in os.environ`) to avoid the
  `${!key:-}` empty-vs-unset bug class.
- `infra/main.bicep` + `infra/main.parameters.json`: new `serviceBusEnabled`
  param mapped from `${SERVICEBUS_ENABLED=}`.
- `infra/modules/containerAppControl.bicep`: `effectiveServiceBusEnabled =
  empty(param) ? controlPlaneEnv.api.SERVICEBUS_ENABLED : param`, applied to the
  api/worker/beat sidecars. Repo JSON default unchanged (`false`).
- `infra/main.json` recompiled.
- `api/tests/test_control_plane_env.py`: the "every guard key is wired into
  Bicep" test now recognises the override-var pattern for `SERVICEBUS_ENABLED`.

### #33 item 2 — detection (backend)
- `api/services/aks/openapi_lb_rbac.py`: new `detect_lb_subnet_rbac_missing`
  (best-effort: reads `default` ns events, matches an `elb-openapi`
  `SyncLoadBalancerFailed` whose message is a subnet AuthorizationFailed) and
  `lb_subnet_rbac_recovery_hint` (`recovery_action: grant_lb_subnet_rbac`).
- `api/routes/aks/openapi.py`: the spec route's IP-missing branch now calls
  `_lb_pending_recovery_hint`, which returns the specific RBAC hint when the
  signature is present and falls back to the generic peering hint otherwise.
  Best-effort and additive; the proxy route is intentionally left on the
  peering hint (it is async and must not block on a sync events read).

### #33 item 3 — SPA grant button (frontend)
- `web/src/api/aks.ts`: `AksLbSubnetRbacResponse` + `aksApi.grantLbSubnetRbac`.
- `web/src/pages/apiReference/GrantLbSubnetRbacButton.tsx`: the button +
  `isGrantLbSubnetRbacRecovery` classifier (mirrors `RepairPeeringButton`).
- `web/src/pages/ApiReference.tsx`: `SpecErrorState` gains `showGrantRbac`; the
  spec error / degraded branches classify grant-first (the generic peering
  `degraded_reason` fallback would otherwise swallow the grant case).

## Persona impact (§12a)

- In scope: rbac (additive grant route already shipped), network/env gate (A).
- No role narrowed; no `roleAssignments` removed from Bicep. A ships
  default-OFF (the JSON default is unchanged; only an explicit azd-env override
  flips it), satisfying Rule 4.
- No `Depends(require_caller)` added to any SSE stream. The detection helper is
  best-effort and only runs on the already-degraded path.

## Validation evidence

- `uv run pytest -q api/tests/test_openapi_lb_subnet_rbac.py` — 21 passed
  (helper, detection, route, deploy integration, spec-route classification).
- `uv run pytest -q api/tests/test_control_plane_env.py` — 10 passed (override
  pattern recognised).
- `uv run pytest -q` across my changed surfaces (route_contracts, openapi
  proxy/pod/tls) — 81 passed.
- quick-deploy override verified: `SERVICEBUS_ENABLED=true` emits `true`, unset
  falls back to the JSON `false`.
- `az bicep build infra/main.bicep` — clean; `infra/main.json` carries the new
  param + `effectiveServiceBusEnabled` if/else.
- `cd web && npm run build` — type-checks and builds; `npx vitest run`
  GrantLbSubnetRbacButton — 5 passed; eslint clean.

## Note on the live deployment

`azd env set SERVICEBUS_ENABLED true` was applied so the next redeploy keeps the
integration on. The live sidecars were already re-enabled out-of-band.
