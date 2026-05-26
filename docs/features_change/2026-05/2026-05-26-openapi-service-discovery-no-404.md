# OpenAPI Service Discovery No-404

## Motivation

The API Reference page probes the `elb-openapi` [AKS](https://learn.microsoft.com/azure/aks/what-is-aks) LoadBalancer service before rendering the OpenAPI spec or deploy panel. A missing service or pending external IP is an expected discovery state, but the monitor route returned HTTP 404, so the HTTP inspector showed routine discovery as an operator-visible API error.

## User-facing change

The service discovery probe now returns HTTP 200 with `available: false`, `external_ip: null`, and `status: missing_or_pending` when the service is absent or still waiting for an IP. The API Reference page still shows the existing deploy panel, but the request no longer appears as a failed call in the inspector.

## API/IaC diff summary

- Updated `GET /api/monitor/aks/service-ip` to return an explicit service discovery state instead of HTTP 404 for missing/pending services.
- Updated the SPA `monitoringApi.serviceIp` type and API Reference page logic to treat a null `external_ip` as the deploy-panel state.
- No infrastructure changes.

## Validation evidence

- Added backend route tests in `api/tests/test_monitor_aks_service_ip.py` for ready, missing, and lookup-exception states.
- Added frontend prefetch coverage for the 200/no-IP response in `web/src/hooks/usePrefetchApiReference.test.ts`.