# OpenAPI Auto Deploy After AKS Start

## Motivation

Starting AKS brought the cluster back online but left the ElasticBLAST OpenAPI execution plane undeployed, so the jobs endpoint degraded with `openapi_not_configured` until a user manually opened the API Reference page and deployed it.

## User-facing change

When AKS start is requested with ACR details, the backend now queues the idempotent `elb-openapi` deployment after the cluster start operation completes. Once the OpenAPI service receives a load balancer IP, the worker stores the runtime base URL in ops Redis so dashboard BLAST API calls can reach it without requiring a static `ELB_OPENAPI_BASE_URL` environment variable.

## API / task diff summary

- `/api/aks/start` derives an `auto_openapi` payload from the existing start request when `acr_name` is available, or accepts an explicit `auto_openapi` object.
- `api.tasks.azure.start_aks` queues `api.tasks.openapi.deploy_openapi_service` after AKS start and returns `openapi_task_id`.
- `api.tasks.openapi.deploy_openapi_service` caches `http://<external-ip>` in ops Redis after deployment.
- `api.services.external_blast` falls back to the cached runtime OpenAPI base URL when `ELB_OPENAPI_BASE_URL` is not configured.
- OpenAPI manifest application now checks the terminal sidecar Azure CLI session and logs in with the Container App Managed Identity when automation runs without a browser-terminal `az login` profile.

## Validation evidence

- `uv run pytest -q api/tests/test_warmup_route.py::test_aks_start_forwards_auto_warmup_payload api/tests/test_warmup_route.py::test_aks_start_forwards_auto_openapi_payload api/tests/test_azure_tasks.py::test_start_aks_enqueues_openapi_after_cluster_start api/tests/test_external_blast_api.py::test_external_blast_base_url_uses_runtime_cache api/tests/test_external_blast_api.py::test_openapi_runtime_round_trip` -> 5 passed.
- `uv run pytest -q api/tests/test_openapi_task.py api/tests/test_warmup_route.py::test_aks_start_forwards_auto_openapi_payload api/tests/test_azure_tasks.py::test_start_aks_enqueues_openapi_after_cluster_start api/tests/test_external_blast_api.py::test_external_blast_base_url_uses_runtime_cache` -> 5 passed.
- `uv run ruff check api` -> all checks passed.
- `uv run pytest -q api/tests` -> 610 passed.
- Built and deployed `acrelbnm5virmqrdi5c.azurecr.io/elb-api:20260518024500-openapi-auto` to the `api`, `worker`, and `beat` sidecars; Container App revision `ca-elb-control--0000050` is healthy and serving `/api/health`.
- Restored ACR network posture after the build: `publicNetworkAccess=Disabled`, `defaultAction=Deny`.
- Ran the OpenAPI deployment task against `rg-elb-01/elb-cluster`; manifest apply succeeded and the service external IP is `20.249.48.153`.
- `curl http://20.249.48.153/openapi.json` -> HTTP 200, OpenAPI `3.1.0`.
- `curl http://20.249.48.153/v1/jobs` -> HTTP 200, `{"jobs":[],"count":0}`.
- API sidecar check: `external_blast._base_url()` returned `http://20.249.48.153` from the runtime cache and `external_blast.list_jobs()` returned an empty jobs list without `openapi_not_configured`.
