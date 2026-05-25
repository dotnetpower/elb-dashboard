# OpenAPI internal LoadBalancer

## Motivation

The API Reference "Try it" proxy injects the `X-ELB-API-Token` admin token when it calls the sibling `elb-openapi` service. The proxy correctly refuses to send that token over plain HTTP to a public LoadBalancer IP, returning `openapi_unsafe_transport`. The OpenAPI deploy manifest therefore needs to create an internal Azure LoadBalancer by default.

## User-facing change

Future `Deploy OpenAPI` runs create the `elb-openapi` Kubernetes Service with the Azure internal LoadBalancer annotation, so the dashboard resolves an RFC1918 Service IP and the API Reference "Try it" calls can proceed without opting into public-token transport.

For the current live environment, the existing public Service was converted to an internal LoadBalancer and the dashboard VNet was peered with the AKS managed VNet so the Container App `api` sidecar can reach the internal IP.

## API/IaC diff summary

- `api.tasks.openapi.manifests.build_manifests` now adds `service.beta.kubernetes.io/azure-load-balancer-internal=true` to the `elb-openapi` Service manifest.
- `api/tests/test_openapi_task.py` asserts the Service remains `LoadBalancer` but carries the internal Azure LoadBalancer annotation.
- `GET /api/aks/openapi/spec` now falls back to the Kubernetes Service proxy when direct HTTP to the internal LoadBalancer is unavailable, keeping local development docs usable.

## Validation evidence

- `uv run pytest -q api/tests/test_openapi_task.py api/tests/test_openapi_proxy_route.py` — 29 passed.
- `uv run ruff check api/routes/aks/openapi.py api/tests/test_openapi_proxy_route.py api/tasks/openapi/manifests.py api/tests/test_openapi_task.py` — passed.
- Local `GET /api/aks/openapi/spec?...` returns `openapi=3.1.0`, `version=3.6.0`, `paths_count=13`, `degraded=false` through the Kubernetes Service proxy fallback.
- Local `/docs` renders `ElasticBLAST API Reference v3.6.0`, `Endpoints 14`, and the System / Cluster / Databases / Jobs groups.
- Live `elb-openapi` Service now carries `service.beta.kubernetes.io/azure-load-balancer-internal=true` and resolves to `10.224.0.15`.
- Live VNet peerings `dashboard-to-aks-openapi` and `aks-to-dashboard-openapi` are `Connected`.
- `az containerapp exec ... --container api --command "curl -sS -m 8 http://10.224.0.15/healthz"` returned `{"status":"ok"}`.
- `POST /api/aks/openapi/token` generated a 43-character token without printing the token value, then `kubectl rollout status deployment/elb-openapi -n default --timeout=90s` completed successfully.