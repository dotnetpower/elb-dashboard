# PLS transition banner — in-banner "Deploy with PLS recreate" button

**Issue**: [#22](https://github.com/dotnetpower/elb-dashboard/issues/22)
(spun out of [#20](https://github.com/dotnetpower/elb-dashboard/issues/20) P3 #10)
**Date**: 2026-05-31
**Layer**: backend route + Celery task + SPA component

## Motivation

`web/src/pages/apiReference/PlsTransitionBanner.tsx` told the operator to set
`OPENAPI_PLS_CONFIRM_RECREATE=1` on the api sidecar and re-trigger the deploy.
That meant leaving the page, opening the Container Apps blade, editing an env
var, restarting the api revision, and *only then* clicking Deploy back on the
`OpenApiDeployPanel`. Negative UX delta vs. a single in-banner button.

## What changed

Backend (`api/`):

- `api/tasks/openapi/deploy.py`
  - `deploy_openapi_service(... , confirm_recreate: bool = False)` —
    new keyword-only param. The PLS first-time-activation gate now reads
    `confirm = bool(confirm_recreate) or env`, so either path unblocks the
    recreate. The legacy `OPENAPI_PLS_CONFIRM_RECREATE` env var still works
    for operators that pre-date the SPA button.
  - Docstring expanded to spell out the OR semantics.
- `api/routes/aks/openapi.py`
  - `aks_openapi_deploy` forwards `confirm_recreate=bool(body.get("confirm_recreate", False))`
    to the Celery task.

SPA (`web/`):

- `web/src/api/aks.ts`
  - `aksApi.deployOpenApi(..., confirmRecreate?: boolean)` —
    new 8th optional positional param. When truthy the request body gains
    `confirm_recreate: true`; otherwise the body shape is unchanged.
- `web/src/pages/apiReference/PlsTransitionBanner.tsx`
  - Banner now accepts optional `acrName`, `acrResourceGroup`, `storageAccount`,
    `storageResourceGroup` props.
  - Renders a "Deploy with PLS recreate" button (`glass-button glass-button--primary`,
    `Wrench` / `Loader2` / `CheckCircle2` lucide icons) that fires
    `useMutation` against `aksApi.deployOpenApi(..., true)`.
  - Disabled state with explanatory hint when `acrName` is missing
    (the route would return 400, so we surface the gap up front).
  - Success state shows "Deploy enqueued" + a pointer back to the
    `OpenApiDeployPanel`. Error state renders the `formatApiError` string
    inline next to the button.
- `web/src/pages/ApiReference.tsx` — the existing banner caller now passes
  the ACR / storage coordinates it already has in scope.

Tests:

- `api/tests/test_openapi_deploy_contract.py` — two new tests:
  - `test_openapi_deploy_route_forwards_confirm_recreate` (route flag flow).
  - `test_openapi_deploy_route_defaults_confirm_recreate_to_false` (default
    path stays opt-out).
- `api/tests/test_openapi_pls_deploy_guard.py` — new
  `test_deploy_openapi_service_accepts_confirm_recreate_kwarg` (signature
  contract: keyword-only, default `False`).
- `web/src/api/aks.test.ts` — two new tests:
  - `forwards confirm_recreate=true when the PLS banner button enqueues a recreate`.
  - `omits confirm_recreate when the flag is false / undefined`.
- The existing `PlsTransitionBanner.test.ts` colour-pipeline tests stay green
  (banner exports unchanged).

## Backward compatibility

- `confirm_recreate` is keyword-only with `default=False`; every existing
  call site continues to work.
- The route ignores absent `confirm_recreate` (falsy default), so legacy
  SPAs / curl scripts post the exact same body.
- The env-var path (`OPENAPI_PLS_CONFIRM_RECREATE=1`) still unblocks the gate.

## Validation

- `uv run pytest -q api/tests/test_openapi_deploy_contract.py api/tests/test_openapi_pls_deploy_guard.py` — 12 passes.
- `cd web && npm test -- --run src/api/aks.test.ts src/pages/apiReference/PlsTransitionBanner.test.ts` — 7 passes.
- Manual smoke (to perform during dogfood): trigger the PLS transition state
  (deploy with `OPENAPI_PLS_ENABLED=1` against a cluster whose `elb-openapi`
  Service lacks `service.beta.kubernetes.io/azure-pls-create: "true"`), then
  click the in-banner button; confirm the Service is recreated and the PLS is
  attached. The `OpenApiDeployPanel` task feed shows progress.

## Acceptance

- Operator can recreate the PLS-attached `elb-openapi` Service from inside
  the banner without leaving the API Reference page or touching env vars.
- Existing PLS transition tests (colour pipeline) stay green.
- No regression on the `disabled / hidden` render branches (banner is hidden
  when the probe is unavailable or `transition_pending=false`).

## Out of scope

- A confirmation modal before recreate. The banner copy already calls out the
  ~1–2 minute external IP outage; surfacing a second modal would be
  double-confirming the same operation. Revisit if the dogfood feedback says
  operators want it.
- Cancelling an in-flight recreate. The existing `cancelOpenApiDeploy` cancel
  route already covers this; the banner button is fire-and-forget so the
  operator should track progress in the `OpenApiDeployPanel` above.
