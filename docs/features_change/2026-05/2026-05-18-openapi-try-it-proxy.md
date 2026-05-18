# 2026-05-18 - OpenAPI Try It Proxy

## Motivation

The API Reference page loaded the deployed `elb-openapi` specification, but the embedded Try It controls called `/api/aks/openapi/proxy`, which the FastAPI sidecar did not implement. In the deployed Container App this fell through to the frontend catch-all and returned `404 unknown api route`.

## User-facing change

- Try It requests on `/docs` now route through the authenticated API sidecar to the running `elb-openapi` LoadBalancer service.
- The proxy returns a structured retryable `503` when the OpenAPI service IP is not available yet.
- Browser bearer tokens are not forwarded to the OpenAPI pod; only request headers needed for content negotiation are passed through.

## API / IaC diff summary

- Added `GET|POST|PUT|PATCH|DELETE /api/aks/openapi/proxy` on the existing AKS router.
- The route resolves the `elb-openapi` service IP through the direct Kubernetes API helper and forwards the request path to that service.
- Added regression tests for successful forwarding, missing service IP handling, invalid path rejection, and auth gating.
- No IaC changes.

## Validation evidence

- `uv run ruff check api/routes/stubs.py api/tests/test_openapi_proxy_route.py api/tests/test_smoke.py`
- `uv run pytest -q api/tests/test_openapi_proxy_route.py api/tests/test_smoke.py` -> 49 passed
- `uv run pytest -q api/tests` -> 626 passed
- `scripts/dev/quick-deploy.sh api` with explicit target env -> deployed `elb-api:20260518155822` to `api`, `worker`, and `beat` on `ca-elb-control`.
- `curl https://ca-elb-control.gentlemeadow-01289e5b.koreacentral.azurecontainerapps.io/api/health` -> 200 from revision `ca-elb-control--0000055`.
- Anonymous `curl` to `/api/aks/openapi/proxy?...&path=%2Fhealthz` -> 401 `missing bearer token`, proving the deployed route is registered and no longer falls through to `404 unknown api route`.