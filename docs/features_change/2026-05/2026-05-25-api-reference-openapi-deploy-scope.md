# API Reference OpenAPI Deploy Scope

## Motivation

The API Reference page could hide the OpenAPI deploy panel when the ElasticBLAST AKS cluster lived outside the dashboard anchor resource group.

## User-facing change

The API Reference page now discovers ElasticBLAST-managed AKS clusters subscription-wide and sends OpenAPI service discovery, deployment status, token, and deploy requests to the selected cluster's actual resource group.

OpenAPI deploy requests also include the dashboard anchor resource group as `storage_resource_group`, so the backend assigns Storage Blob Data Contributor on the actual Storage account scope instead of assuming the Storage account lives beside the AKS cluster.

## API/IaC diff summary

No API or IaC changes. The frontend reuses the existing subscription-wide `/api/monitor/aks` response and existing OpenAPI deploy/status endpoints.

## Validation evidence

- `npm run test -- usePrerequisites useLatestBlastJob clusterContext aks usePrefetchApiReference`
- `npm run build`
- `uv run pytest -q api/tests/test_openapi_deploy_contract.py api/tests/test_openapi_task.py`
- `uv run ruff check api/tests/test_openapi_deploy_contract.py api/tests/test_openapi_task.py`