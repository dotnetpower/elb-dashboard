---
title: Copy curl in the API Reference emits X-ELB-API-Token instead of MSAL bearer
description: The API Reference "Copy curl" button now always emits X-ELB-API-Token, $ELB_API_TOKEN placeholder — the shell command is portable to a peer-VNet automation caller and no longer bakes in a 60-minute MSAL bearer that expires as soon as the user leaves the page. Send Request (browser execution) is unchanged.
tags:
  - auth
  - ui
  - operate
---

# Copy curl in the API Reference emits `X-ELB-API-Token`

## Motivation

The API Reference page (`Tools → API`) has always had a per-endpoint
**Copy curl** button that generates a shell-safe `curl` command mirroring
what the browser Send Request would send. Historically that button pulled
a **live MSAL access token** from the browser's [MSAL](https://learn.microsoft.com/entra/identity-platform/msal-overview)
session via `getApiAccessToken()` and inlined it as
`Authorization: Bearer …`, falling back to a `$AAD_TOKEN` placeholder
when the user was not signed in.

That default is misaligned with how a peer-VNet automation caller
actually consumes the copied command: they paste it on a VM and run it
under a shared token that must live longer than one MSAL access token
(60 minutes). A copied Bearer expires almost immediately; a placeholder
would work in principle but the operator still has to hand-edit the
header name — every time.

With the universal M2M shared-token path shipped in
[2026-07-01-m2m-token-universal.md](2026-07-01-m2m-token-universal.md),
the dashboard now accepts `X-ELB-API-Token` on every `require_caller`
route when the `ALLOW_OPENAPI_TOKEN_AUTH` gate is on. The natural next
step is to make **Copy curl reflect that surface** by default.

## User-facing change

* The **Copy curl** button in every API Reference endpoint (both the
  Core control-plane / dashboard-API routes and the cluster-scoped
  proxy routes) now emits:

  ```shell
  curl -X <METHOD> '<url>' \
    -H 'X-ELB-API-Token: $ELB_API_TOKEN' \
    …
  ```

  The user sets `ELB_API_TOKEN` on the calling host and runs the
  command as-is. The dashboard requires
  `ALLOW_OPENAPI_TOKEN_AUTH=true` on the api sidecar for this to
  authenticate; otherwise the copied command 401s with
  `missing bearer token` — that failure is a signal to enable the
  gate, not to change what the button emits.

* **Send Request** (browser-side execution from the same panel) is
  unchanged. It still uses the browser's MSAL session bearer via the
  existing `fetchApi` / `fetchApiRawNoRedirect` client. Users signed
  into the SPA continue to test endpoints under their own Azure AD
  identity without doing anything special.

* Endpoints whose Try It is aimed at the upstream cluster host
  directly (`baseUrl` set, no proxy) continue to emit no auth
  header — that surface has its own auth posture and was never
  Bearer-shaped from Copy curl anyway.

## API / IaC diff summary

* [web/src/hooks/useOpenApiExecutor.ts](https://github.com/dotnetpower/elb-dashboard/blob/main/web/src/hooks/useOpenApiExecutor.ts) —
  `buildCurl` no longer accepts `bearerToken`; both `dashboardApi`
  and `proxyInfo` branches push
  `["X-ELB-API-Token", "$ELB_API_TOKEN"]` instead of
  `Authorization: Bearer …`. `copyCurl` drops the `getApiAccessToken()`
  fetch and the associated import.
* [web/src/hooks/useOpenApiExecutor.test.ts](https://github.com/dotnetpower/elb-dashboard/blob/main/web/src/hooks/useOpenApiExecutor.test.ts) —
  tests reshaped: proxy and dashboardApi paths must emit
  `X-ELB-API-Token: $ELB_API_TOKEN` and must **not** emit any
  `Authorization` header or `$AAD_TOKEN` placeholder. Added an
  explicit dual-mode assertion so a future regression that
  re-introduces the Bearer surface fails loudly.
* No backend / Bicep changes. The auth acceptance for
  `X-ELB-API-Token` already exists in [api/auth.py](https://github.com/dotnetpower/elb-dashboard/blob/main/api/auth.py)
  behind the `ALLOW_OPENAPI_TOKEN_AUTH` gate (default OFF).

## Validation

* `cd web && npm test -- --run useOpenApiExecutor` — 17 assertions
  green after the reshape (proxy + dashboardApi both emit
  `X-ELB-API-Token: $ELB_API_TOKEN`, direct upstream mode still emits
  no auth header, single-quote escaping still POSIX-safe).
* `cd web && npm run build` — clean build, no unused-import
  regressions after dropping `getApiAccessToken`.
* Deployed to the customer Container App: frontend revision
  `ca-elb-dashboard--0000201` (image tag `20260701103115`).
