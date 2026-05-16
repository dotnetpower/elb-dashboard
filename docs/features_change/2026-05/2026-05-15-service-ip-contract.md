# 2026-05-15 — Align `/api/monitor/aks/service-ip` with SPA contract

## Motivation

Earlier today the `/docs` page (API Reference) was stuck on
"Discovering OpenAPI service on AKS…" forever. Root cause: the previous
service-ip fix wrapped the raw `str | None` from `k8s_get_service_ip` in
`{"ip": ...}` to satisfy FastAPI's response model. That made the endpoint
return HTTP 200 even when the LoadBalancer had no IP yet.

The SPA (`web/src/api/monitoring.ts` and `web/src/pages/ApiReference.tsx`)
expects:

```ts
type ServiceIpResponse = { service_name: string; external_ip: string };
```

and uses TanStack Query's `isError` flag to decide whether to render the
`OpenApiDeployPanel`. With a 200 response containing `{"ip": null}`, the
SPA computed `baseUrl = "http://undefined"` (truthy), kicked off the spec
fetch against a meaningless URL, and never reached the error branch — so
the deploy panel never appeared.

## User-facing change

- `/docs` now correctly shows the **OpenAPI service not found** card with
  the **Deploy** and **Retry Discovery** buttons when the `elb-openapi`
  Service does not yet have a LoadBalancer IP.
- Once the Service comes up with an external IP, the page will render the
  live OpenAPI specification as before.

## API / IaC diff summary

- `api/routes/monitor.py:aks_service_ip`
  - Returns `{"service_name": ..., "external_ip": ...}` on success
    (matches the SPA's typed client).
  - Raises `HTTPException(404, detail={"code": ..., "service_name": ...})`
    when the Service is missing or has no LoadBalancer ingress yet — this
    is the signal the SPA needs to flip into the deploy-panel state.
- No infra changes.

## Validation evidence

- `curl -sw '\nHTTP %{http_code}\n' …/api/monitor/aks/service-ip…` →
  `{"code":"service_no_external_ip","service_name":"elb-openapi"}` HTTP 404.
- Browser snapshot of `http://127.0.0.1:8090/docs` after the fix shows the
  **OpenAPI service not found** card with **Deploy** + **Retry Discovery**
  buttons (replacing the infinite "Discovering…" spinner).
- `uv run pytest -q api/tests` → 123 passed.
