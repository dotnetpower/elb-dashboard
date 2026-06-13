---
title: "OpenAPI proxy Try-It surfaces LB subnet-RBAC recovery hint (#33)"
description: "The API Reference Try-It proxy now classifies an LB-pending elb-openapi as the BYO node-subnet RBAC gap and offers the one-click grant, closing the last #33 gap."
tags:
  - blast
  - operate
---

# OpenAPI proxy "Try it" surfaces the LB subnet-RBAC recovery hint (#33)

## Motivation

GitHub issue #33 ("elb-openapi internal LB stuck `<pending>` after manual
cluster recreate") shipped detection + a one-click "Grant LB subnet RBAC"
recovery for the **spec** route, but the **proxy ("Try it")** route was left on
the generic VNet-peering hint. That route is `async`, and the original
`detect_lb_subnet_rbac_missing` does a blocking Kubernetes events read that
would stall the event loop. This was the single remaining gap that kept #33
open.

## User-facing change

When the `elb-openapi` internal LoadBalancer has no IP (the #33 signature) and
an operator clicks **Try it** on an API Reference endpoint, the proxy now
classifies the cause:

- If the `default`-namespace events carry an `elb-openapi`
  `SyncLoadBalancerFailed` whose message is a subnet `AuthorizationFailed`, the
  503 response carries `recovery_action: grant_lb_subnet_rbac`, and the Try-It
  panel renders the **Grant LB subnet RBAC** button (idempotent POST to
  `/api/aks/openapi/lb-subnet-rbac`).
- Otherwise it degrades to the existing **Repair VNet peering** hint —
  unchanged behaviour.

The 502 "upstream request failed mid-flight" branch is intentionally left on
the peering hint: that is a genuine connectivity break, not an LB-pending
state.

## API / IaC diff summary

Backend (additive, best-effort):

- `api/routes/aks/openapi.py` — new `_lb_pending_recovery_hint_async`, an
  event-loop-safe wrapper that offloads `_lb_pending_recovery_hint` to a worker
  thread (`asyncio.to_thread`) and degrades to the peering hint on any failure.
- `api/routes/aks/openapi_proxy.py` — the LB-IP-missing 503 branch now awaits
  the async hint instead of the static peering hint.

Frontend (additive):

- `web/src/pages/apiReference/EndpointCard.tsx` — the Try-It error path renders
  `GrantLbSubnetRbacButton` when the proxy response classifies as
  `grant_lb_subnet_rbac` (taking precedence over the peering button), reusing
  the existing classifier + component.

## Validation evidence

- `uv run pytest -q api/tests/test_openapi_proxy_route.py api/tests/test_openapi_lb_subnet_rbac.py` → 49 passed (new `test_openapi_proxy_503_returns_rbac_hint_when_detected`; existing 503 test pinned to detection-False to stay deterministic).
- `uv run ruff check api/routes/aks/openapi.py api/routes/aks/openapi_proxy.py` → clean.
- `cd web && npm run build` → success; `npx vitest run GrantLbSubnetRbacButton RepairPeeringButton` → 11 passed; `eslint EndpointCard.tsx` → clean.
