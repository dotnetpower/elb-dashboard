---
title: Copy curl inlines the real shared token + Bicep persistence
description: A new /api/settings/openapi-token route exposes the shared M2M token to authenticated dashboard callers, the SPA's Copy curl inlines the real value in place of $ELB_API_TOKEN, and the Container App secret + api sidecar env are now declared in Bicep so the wiring survives every azd provision.
tags:
  - auth
  - ui
  - infra
  - operate
---

# Copy curl inlines the real shared token + Bicep persistence

## Motivation

Two follow-ups landed together after the operator flip to universal M2M
shared-token auth:

1. **"Copy curl" was still emitting the `$ELB_API_TOKEN` placeholder**, so
   the copied command required manual substitution on the caller's host —
   a papercut that defeats the "paste-and-run" idea.
2. **The wiring for the shared token was manual** (`az containerapp secret
   set` + `az containerapp update`), so the next `azd provision` would drop
   the secret + env and revert `ALLOW_OPENAPI_TOKEN_AUTH` to the JSON
   default, silently killing every M2M caller.

## User-facing change

* **New route**: `GET /api/settings/openapi-token`
  * Authenticated (any `require_caller` persona — Reader / Contributor /
    Owner / dev-bypass all accepted, per the operator "no strict security"
    policy that shipped with the universal M2M path).
  * Response: `{ token: string, gate_enabled: bool }`. `token` is the
    value the auth gate actually accepts (resolved via
    `api.auth._resolve_expected_openapi_token`); `""` when the deployment
    has no shared token configured. `gate_enabled` reflects
    `ALLOW_OPENAPI_TOKEN_AUTH`.
  * Trade-off: any authenticated user (including Reader) can now read the
    shared admin token. Documented; matches the operator policy that
    dashboard log-in is the trust boundary. To re-tighten later, swap
    `require_caller` for a Contributor role check in the route body.

* **Copy curl**: the API Reference "Copy curl" button now fetches the
  route and inlines the real token value in place of the placeholder.
  When the fetch fails (unauthenticated / gate off / no token) it
  transparently falls back to the `$ELB_API_TOKEN` placeholder so the
  copied command remains a useful template.

* **Bicep persistence**: shared token now flows through IaC.
  * New `@secure() param openApiSharedToken` on
    `infra/main.bicep` + `containerAppControl.bicep`.
  * `infra/main.parameters.json` binds it to the azd env var
    `AZURE_OPENAPI_SHARED_TOKEN`.
  * When non-empty the module registers the Container App secret
    `elb-openapi-api-token` and adds
    `{ name: 'ELB_OPENAPI_API_TOKEN', secretRef: 'elb-openapi-api-token' }`
    to the api sidecar env; when empty the secret + env are omitted (the
    M2M path then rejects every `X-ELB-API-Token` header — fail-safe).
  * `infra/control-plane-env.json` flips
    `ALLOW_OPENAPI_TOKEN_AUTH` from `"false"` to `"true"` so the universal
    M2M gate is on by default for this deployment. Reverting to
    `"false"` restores MSAL-bearer-only behaviour.

## Operator flow

To wire the shared token for a deployment:

```bash
azd env set AZURE_OPENAPI_SHARED_TOKEN "<64-hex or base64 token>"
azd provision   # Bicep now owns the secret + env
```

To disable the M2M path entirely for a deployment: leave
`AZURE_OPENAPI_SHARED_TOKEN` unset AND set `ALLOW_OPENAPI_TOKEN_AUTH`
back to `"false"` in `infra/control-plane-env.json`. Either alone is
sufficient — an empty token with the gate on still rejects every M2M
request.

## API / IaC diff summary

* Backend:
  * [api/routes/settings/openapi_token.py](https://github.com/dotnetpower/elb-dashboard/blob/main/api/routes/settings/openapi_token.py) —
    new route.
  * [api/routes/settings/__init__.py](https://github.com/dotnetpower/elb-dashboard/blob/main/api/routes/settings/__init__.py) —
    wire it under `/api/settings/openapi-token`.
  * [api/tests/test_settings_openapi_token.py](https://github.com/dotnetpower/elb-dashboard/blob/main/api/tests/test_settings_openapi_token.py) —
    4 assertions (real-value round-trip, empty-when-unconfigured,
    gate-off reporting, anonymous 401).
* Frontend:
  * [web/src/api/settings.ts](https://github.com/dotnetpower/elb-dashboard/blob/main/web/src/api/settings.ts) —
    `settingsApi.getOpenApiToken()` + `SharedApiTokenStatus` (named to
    avoid clashing with the existing `OpenApiTokenStatus` in `api/aks.ts`
    which describes the in-cluster elb-openapi admin token).
  * [web/src/hooks/useOpenApiExecutor.ts](https://github.com/dotnetpower/elb-dashboard/blob/main/web/src/hooks/useOpenApiExecutor.ts) —
    `copyCurl` fetches the token before building the curl; `buildCurl`
    now accepts an optional `m2mToken` and inlines it into the
    `X-ELB-API-Token` header.
* Infra:
  * [infra/main.bicep](https://github.com/dotnetpower/elb-dashboard/blob/main/infra/main.bicep) +
    [infra/modules/containerAppControl.bicep](https://github.com/dotnetpower/elb-dashboard/blob/main/infra/modules/containerAppControl.bicep) —
    `openApiSharedToken` param + conditional secret + conditional api
    sidecar env.
  * [infra/main.parameters.json](https://github.com/dotnetpower/elb-dashboard/blob/main/infra/main.parameters.json) —
    azd env binding.
  * [infra/control-plane-env.json](https://github.com/dotnetpower/elb-dashboard/blob/main/infra/control-plane-env.json) —
    `ALLOW_OPENAPI_TOKEN_AUTH` default now `"true"`.
  * Regenerated `infra/main.json` + `infra/modules/containerAppControl.json`.

## Validation

* Backend: `uv run pytest -q api/tests/test_settings_openapi_token.py
  api/tests/test_route_contracts.py api/tests/test_m2m_token_universal.py`
  — 18 tests green.
* Frontend: `cd web && npm test -- --run useOpenApiExecutor` — 19 tests
  green (added `inlines the real M2M token value when provided` and
  `falls back to the placeholder when m2mToken is undefined or empty`).
* Bicep: `az bicep build --file infra/main.bicep` — clean.
* Deployed to the customer Container App:
  * api revision `ca-elb-dashboard--0000205` (image tag `20260701105504`),
    frontend revision `ca-elb-dashboard--0000206` (image tag
    `20260701110317`).
  * End-to-end curl against `GET /api/settings/openapi-token` with the
    shared token — HTTP 200, real token returned, `gate_enabled=true`.
