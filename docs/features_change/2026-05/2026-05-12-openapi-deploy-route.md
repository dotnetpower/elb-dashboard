# POST /api/aks/openapi/deploy + ApiReference Deploy panel

**Date**: 2026-05-12
**Scope**: `api/orchestrators/provision_aks.py`, `api/function_app.py`,
`web/src/api/endpoints.ts`, `web/src/pages/ApiReference.tsx`

## Motivation

The OpenAPI service (`elb-openapi`) is deployed once during
`provision_aks_orchestrator`. If the user later:

- rebuilds the `elb-openapi` image with a new tag,
- deletes the namespace by accident, or
- imports a workspace whose cluster predates the OpenAPI deployment,

there was no way to redeploy the service without re-provisioning the
whole cluster. The API Reference page just showed "OpenAPI service
not found — deploy it from the ACR card on the Dashboard", but the
Dashboard never had such an action — the cluster orchestrator was
the only producer.

## User-facing change

- New **Deploy elb-openapi** button on the API Reference page,
  rendered when the cluster is reachable but the service IP query
  returns 404. Single click triggers the deploy and starts polling
  the service-IP endpoint until the LoadBalancer surfaces a public
  IP (typically 30–90 s).
- Button is disabled with an explanatory tooltip when:
  - the `elb-openapi` image has not been built yet, or
  - the cluster's ACR is not configured.
- Errors surface inline (full message) instead of disappearing into
  a toast.

## API / IaC diff summary

`api/orchestrators/provision_aks.py`:
- New `deploy_openapi_orchestrator` runs only the workload-identity
  setup activity (idempotent) and the OpenAPI deployment activity.
  Does NOT create AKS, VNet, or any cluster-level infrastructure.
  Returns `{cluster_name, resource_group, workload_identity,
  openapi_deploy, status}`.

`api/function_app.py`:
- New HTTP route `POST /api/aks/openapi/deploy` requires a bearer
  token, validates `subscription_id`, `resource_group`,
  `cluster_name` against the standard regex set, and starts the new
  orchestrator via the Durable client. Returns the standard
  check-status response.
- Registers `deploy_openapi_orchestrator` next to the existing
  `provision_aks_orchestrator` registration.

`web/src/api/endpoints.ts`:
- New `aksApi.deployOpenApi(sub, rg, cluster, acr, storageAccount)`
  POSTs to `/aks/openapi/deploy`.

`web/src/pages/ApiReference.tsx`:
- Replaces the static "OpenAPI service not found" alert with a new
  `OpenApiDeployPanel` component:
  - Imports `aksApi` and the existing `formatApiError` helper.
  - Reads ACR from saved config (`acrName`, `acrResourceGroup`) and
    queries the registry to determine `hasOpenApiImage`.
  - Renders the Deploy button, success banner, error banner, and a
    secondary "Retry Discovery" action.
  - On click, calls `aksApi.deployOpenApi`, then schedules a polling
    loop that calls `onRetry()` (parent's `svcQuery.refetch`) every
    10 s for up to 3 minutes.

## Validation evidence

- `pytest -q api/tests/` → 13 passed.
- `npx tsc --noEmit` (web) → clean.
- `npx vite build --mode production` → succeeded.
- API and SPA already deployed.
- Pending: user verifies the Deploy button against the production
  cluster (cluster up, image built, service deleted).
