# OpenAPI peering recovery — surface in-product

## Motivation

When the dashboard's API Reference page hangs at "Sending..." on
`/healthz` or shows an empty docs surface, the root cause is almost
always a missing VNet peering between the dashboard platform VNet and
the AKS auto-VNet. The auto-peering shipped in `provision_aks`
([2026-05-27 - AKS VNet auto-peering](2026-05-27-aks-vnet-auto-peering.md))
fixes new clusters, but it left three gaps:

1. Clusters created before the auto-peering step shipped never get peered
   unless an operator runs `scripts/dev/peer-cluster-network.sh` by hand.
2. The SPA showed the raw HTTP error / a generic "Failed to load
   openapi.json" with no recovery affordance — the `/api/aks/peer-with-
   platform` recovery route existed but no UI called it.
3. The shell script's only auth path (`az account get-access-token
   --resource api://$API_CLIENT_ID`) fails with AADSTS65001 unless the
   user is pre-consented to the SPA's API scope — a friction we hit
   ourselves on 2026-05-27 and had to bypass with direct `az network
   vnet peering create` calls.

## User-facing change

- API Reference page now detects `recovery_action: peer_with_platform`
  in the spec / Try It error payload and renders an inline
  **Repair VNet peering** button (lucide-react `Wrench` icon, glass
  primary). One click POSTs to `/api/aks/peer-with-platform`; on
  success the spec and the failed Try It re-fetch automatically.
- The same affordance also fires when the spec route degrades to a
  200 placeholder (`degraded_reason ∈ {openapi_endpoint_unreachable,
  openapi_service_not_reachable}`) — previously that case was silent.
- `scripts/dev/peer-cluster-network.sh` automatically falls back to
  direct `az network vnet peering create` if the access-token
  acquisition or the dashboard call fails. Operators with just
  Network Contributor on both VNets can recover without the App
  Registration scope consent dance.

## API / IaC diff summary

- `api/routes/aks/openapi.py` — new helper `_peering_recovery_hint()`
  merges `recovery_action: "peer_with_platform"` + `recovery_hint`
  into:
  - the 503 detail of `aks_openapi_proxy` when the Service IP cannot
    be resolved,
  - the 502 detail of `aks_openapi_proxy` when the upstream request
    raises an `httpx.RequestError`,
  - the degraded-200 payloads of `aks_openapi_spec` (`degraded_reason`
    `openapi_service_not_reachable` and `openapi_endpoint_unreachable`).
  Existing fields (`code`, `message`, `retryable`, `degraded`,
  `degraded_reason`) are unchanged — additive only.
- `web/src/api/aks.ts` — new `AksPeerWithPlatformResponse` interface
  and `aksApi.peerWithPlatform(subscriptionId, rg, clusterName)`
  binding for the existing `POST /api/aks/peer-with-platform` route.
- `web/src/pages/apiReference/RepairPeeringButton.tsx` — new component
  (button + inline error / skipped / success status). Also exports
  `isPeerWithPlatformRecovery(payload)`: a small pure detector used
  by both the page-level `SpecErrorState` and the per-card Try-It
  response viewer.
- `web/src/pages/ApiReference.tsx` — `SpecErrorState` accepts repair
  props; additionally rendered when `specQuery.data` is a degraded
  spec carrying the recovery flag.
- `web/src/pages/apiReference/EndpointCard.tsx` — when a Try-It
  response has status 502/503 and the body's JSON carries the
  recovery hint, render the same `RepairPeeringButton` underneath
  the response viewer with `onResolved` triggering `execute()`.
- `scripts/dev/peer-cluster-network.sh` — new `resolve_dashboard_vnet_id`
  + `direct_az_peer` functions. The dashboard call path is unchanged
  on the happy path; on token-acquire failure, HTTP 401/403, or any
  non-200 HTTP code the script logs the fallback transition and
  invokes `direct_az_peer`. Idempotent: AlreadyExists / Conflict are
  treated as success in both directions.

No infrastructure changes (Bicep, azd) — recovery flow is pure
application code and a shell script.

## Validation evidence

- Backend: `uv run pytest -q api/tests` — `1624 passed in 34.21s`
  (was 1621, +3 new tests):
  `test_openapi_proxy_502_includes_peer_with_platform_recovery_hint`,
  `test_openapi_spec_degraded_payload_carries_recovery_hint`,
  augmented assertion in
  `test_openapi_proxy_returns_503_when_service_ip_missing`.
- Frontend: `cd web && npm test -- --run` — `363 passed` (was 357,
  +6 new tests in `RepairPeeringButton.test.ts`).
- Lint: `uv run ruff check api/routes/aks/openapi.py
  api/tests/test_openapi_proxy_route.py` — clean.
- Build: `cd web && npm run build` — clean (existing 500 kB chunk
  warning unchanged).
- Script: `bash -n scripts/dev/peer-cluster-network.sh` — clean.
- Live: verified on 2026-05-27 03:11–03:18 that the peering script
  successfully repaired `elb-cluster-01` (dashboard VNet ↔
  `aks-vnet-23268255`, both `Connected`). The same operation is now
  one click away in the SPA without leaving the page.

## Consumer search

`api/services/blast/external_jobs.py::_exception_reason` and
`_exception_is_transport_failure` read `detail.get("code")` only, so
the additive `recovery_action` / `recovery_hint` fields are ignored —
no behaviour change for the BLAST submit fallback path. No
exact-equality assertions on the openapi error detail exist in the
test suite.
