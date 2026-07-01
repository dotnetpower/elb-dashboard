---
title: Universal M2M shared-token auth across every require_caller route
description: Fold the opt-in X-ELB-API-Token path into require_caller itself so a peer-VNet automation caller uses one credential across every route — read AND write — when ALLOW_OPENAPI_TOKEN_AUTH=true. Default OFF preserves existing MSAL-only behaviour.
tags:
  - auth
  - security
  - operate
---

# Universal M2M shared-token auth across every `require_caller` route

## Motivation

The [opt-in shared-token gate `ALLOW_OPENAPI_TOKEN_AUTH`](2026-06-15-openapi-token-shared-auth.md)
originally scoped the `X-ELB-API-Token` path to two **read-only** OpenAPI
database routes, on the reasoning that the shared admin token has no Azure
RBAC gate and therefore should not reach cost-bearing / mutating actions.

Operator policy (2026-07) is that a peer-VNet automation caller reaching the
control plane over a network path the operator already trusts (private
ingress, IP allowlist, or VNet peering) should manage **one** credential
across the whole API surface — read AND write — the way an [APIM](https://learn.microsoft.com/azure/api-management/api-management-key-concepts)
subscription key gates a full backend. Splitting auth into "shared token for
2 reads, MSAL bearer for everything else" forces the automation caller into
an interactive `az login` flow they cannot reasonably run.

## User-facing change

When `ALLOW_OPENAPI_TOKEN_AUTH=true` is set on the api sidecar:

* **Every** [`require_caller`](https://github.com/dotnetpower/elb-dashboard/blob/main/api/auth.py)-gated
  route (all of `/api/*` except the download-token / dev-bypass paths) now
  accepts `X-ELB-API-Token: <shared>` in place of an `Authorization: Bearer …`
  header. Match => synthetic M2M identity (object id `openapi-token-caller`).
* A present-but-wrong `X-ELB-API-Token` returns `401 invalid X-ELB-API-Token`
  without falling back to bearer — a stale token surfaces as itself rather
  than a confusing "missing bearer token" error.
* A missing token header still runs the standard MSAL-bearer / dev-bypass
  path unchanged, so a browser session and an M2M caller can share the same
  ingress.

When `ALLOW_OPENAPI_TOKEN_AUTH` is unset / `false` (the default):

* The `X-ELB-API-Token` header is ignored entirely.
* Existing MSAL-bearer behaviour is preserved exactly — no route changes
  its auth surface.

## Operator responsibility (charter §12 impact)

The shared token has no Azure RBAC gate, so with the universal M2M path
enabled the network boundary is what limits the caller. Enable this gate
only when at least one of the following holds:

* Container App ingress is `external: false` (VNet-internal); or
* `ingress.ipSecurityRestrictions` allow-lists the peer VNet CIDRs and any
  legitimate operator IPs; or
* Callers reach the ingress via a private-endpoint / Front Door path they
  control.

Do NOT combine `ALLOW_OPENAPI_TOKEN_AUTH=true` with the current default
`external: true` public ingress + no IP restriction. The charter §12 storage
posture (`publicNetworkAccess: Disabled`) is unaffected — the dashboard still
proxies uploads / downloads through the api sidecar and never issues SAS
tokens to a browser.

## API / IaC diff summary

* [api/auth.py](https://github.com/dotnetpower/elb-dashboard/blob/main/api/auth.py) —
  `require_caller` now accepts an `x_elb_api_token` `Header` parameter and,
  when the gate is on and the header is present, validates it before the
  bearer branch. `require_caller_or_openapi_token` is kept as a
  same-object alias so existing route decorators and imports continue to
  work with FastAPI's per-dependency request-scoped cache.
* [api/routes/aks/openapi_databases.py](https://github.com/dotnetpower/elb-dashboard/blob/main/api/routes/aks/openapi_databases.py) —
  module docstring updated: the two catalogue routes are still read-only,
  but they no longer authenticate more strictly than the rest of the API
  because the alias resolves to `require_caller` itself.
* [docs/operate/feature-gates.md](https://github.com/dotnetpower/elb-dashboard/blob/main/docs/operate/feature-gates.md) —
  `ALLOW_OPENAPI_TOKEN_AUTH` row rewritten to reflect the universal scope
  and the operator responsibility for ingress control.
* No Bicep / infra changes. The gate default in
  [infra/control-plane-env.json](https://github.com/dotnetpower/elb-dashboard/blob/main/infra/control-plane-env.json)
  stays `"false"` — this change is a behaviour extension of the ON state,
  not a default flip.

## Validation

* `uv run pytest -q api/tests/test_m2m_token_universal.py` — 9 new
  assertions covering (a) `require_caller` accepts / rejects / ignores the
  shared token depending on the gate, (b) `require_caller_or_openapi_token`
  is a same-object alias, (c) `AUTH_DEV_BYPASS` still wins over M2M, and
  (d) a real mutating route (`POST /api/aks/openapi/deploy`) authenticates
  by shared token alone (passes to the route body's `400
  missing_parameters` branch, which proves the auth gate did not 401).
* `uv run pytest -q api/tests/test_aks_openapi_databases.py` — the pre-
  existing shared-token tests on the read-only catalogue routes still
  green (37 passed) so the alias refactor did not regress that surface.
* `uv run ruff check api` — clean.
