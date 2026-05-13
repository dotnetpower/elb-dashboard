# OpenAPI Deploy Progress Persistence

## Motivation

Deploying `elb-openapi` from the API page starts a Durable Functions orchestration, but the frontend only tracked progress in component-local state. A browser refresh or component remount could lose the in-progress deployment and make the API page appear to restart discovery.

## User-facing change

The API page now persists the OpenAPI deployment instance id per cluster and restores it after refresh. While the deployment is running, the panel polls Durable status and shows the current phase. After the orchestration completes successfully, the page keeps retrying service discovery until the `elb-openapi` pod and service are visible.

## API/IaC diff summary

- Added `GET /api/aks/openapi/deploy/{instance_id}/status` for authenticated Durable status polling.
- Added `aksApi.openApiDeployStatus()` to the frontend client.
- Reworked `OpenApiDeployPanel` to persist/restore deploy status with localStorage and TanStack Query polling.
- No infrastructure changes.

## Validation evidence

- `npm run build` passed.
- `python -m py_compile api/routes/aks.py` passed.
- `git diff --check` passed for the changed files.
- Local Function route smoke: `GET /api/aks/openapi/deploy/not-valid/status` returned HTTP 400 with `invalid instance_id`, confirming the route is registered.
- Local browser smoke: `http://localhost:8090/docs` rendered the API Reference page.