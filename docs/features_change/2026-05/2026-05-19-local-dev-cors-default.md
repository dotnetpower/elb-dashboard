# Local Dev CORS Default

## Motivation

The Vite development server runs at `http://localhost:8090` while the host-mode FastAPI API runs at `http://localhost:8085`. The web client sets `x-client-request-id`, so browsers send a CORS preflight before dashboard API calls. Without a local CORS default, FastAPI returned 404 for those `OPTIONS` requests and the dashboard surfaced generic `Network error` messages.

## User-facing change

- `scripts/dev/local-run.sh api` now defaults `CORS_ALLOW_ORIGINS` to `http://localhost:8090,http://127.0.0.1:8090` unless explicitly overridden.
- The VS Code `API: FastAPI (uvicorn)` debug configuration now injects the same `CORS_ALLOW_ORIGINS` value, so launching the API from the debugger behaves like the local task path.

## API / IaC diff summary

- No API contract change.
- No IaC change.
- Local development process environment only.

## Validation evidence

- `bash -n scripts/dev/local-run.sh`
- `curl -i -X OPTIONS 'http://127.0.0.1:8085/api/monitor/aks?subscription_id=...&resource_group=rg-elb-05071' -H 'Origin: http://localhost:8090' -H 'Access-Control-Request-Method: GET' -H 'Access-Control-Request-Headers: x-client-request-id'` returned `200 OK` with `access-control-allow-origin: http://localhost:8090`.
- `curl -i 'http://127.0.0.1:8085/api/monitor/sidecars' -H 'Origin: http://localhost:8090'` returned `200 OK` with `access-control-allow-origin: http://localhost:8090`.