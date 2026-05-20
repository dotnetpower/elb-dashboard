# OpenAPI API token UI

## Motivation

External OpenAPI clients need the `X-ELB-API-Token` value, but the dashboard previously only consumed `ELB_OPENAPI_API_TOKEN` when it was already configured somewhere else.

## User-facing change

The API Reference page now shows an API Token panel for the deployed `elb-openapi` service. Operators can view, copy, generate, and regenerate the `X-ELB-API-Token` value from the browser. The OpenAPI update panel now appears only when the deployed `elb-openapi` image tag differs from the dashboard-pinned tag.

## API / IaC diff summary

- Added `GET /api/aks/openapi/token` to read the token from the AKS `elb-openapi` deployment env.
- Added `POST /api/aks/openapi/token` to generate or rotate `ELB_OPENAPI_API_TOKEN` on the `openapi` container.
- Added `GET /api/aks/openapi/deployment` to read the deployed `elb-openapi` container image tag from AKS.
- Added a runtime Redis token cache so dashboard-to-OpenAPI requests can reuse the generated token without restarting the API sidecar.
- OpenAPI redeploy/update preserves the cached token in the generated Kubernetes deployment manifest.
- No Bicep or Container App layout changes.

## Validation evidence

- `uv run pytest -q api/tests/test_openapi_deployment.py api/tests/test_openapi_token.py api/tests/test_openapi_task.py api/tests/test_route_contracts.py api/tests/test_external_blast_api.py` — pending rerun after deployment-tag visibility change.
- `uv run ruff check api/services/openapi_deployment.py api/tests/test_openapi_deployment.py api/tasks/openapi/__init__.py api/tests/test_openapi_task.py api/services/openapi_token.py api/services/openapi_runtime.py api/services/external_blast.py api/routes/aks/openapi.py api/tests/test_openapi_token.py api/tests/test_route_contracts.py` — pending rerun after deployment-tag visibility change.
- `cd web && npm run build` — passed; Vite emitted the existing large-chunk warning.
