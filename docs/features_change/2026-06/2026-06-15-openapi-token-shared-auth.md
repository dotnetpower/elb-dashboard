---
title: Opt-in shared-token auth for the read-only OpenAPI database routes
description: Let the cluster-independent GET /api/aks/openapi/databases routes also accept the shared elb-openapi X-ELB-API-Token (default-OFF, read-only only) so a caller manages one credential instead of two.
tags:
  - auth
  - security
  - blast
---

# Opt-in shared-token auth for the read-only OpenAPI database routes

## Motivation

The cluster-independent database catalogue routes
(`GET /api/aks/openapi/databases` and `/{db_name}`) mirror the in-cluster
`elb-openapi` `/v1/databases*` reads on the always-on dashboard `api` sidecar.
Until now they authenticated **only** via the [MSAL](https://learn.microsoft.com/entra/identity-platform/msal-overview)
bearer, while `elb-openapi` itself authenticates via the shared
`X-ELB-API-Token`. A caller wanting both surfaces had to manage **two**
credentials.

A maintainer asked whether the dashboard could share the `elb-openapi` token so
one credential covers both. It can: the api sidecar already resolves that token
(from its `ELB_OPENAPI_API_TOKEN` env, then the Redis runtime cache) to inject it
when proxying to `elb-openapi`, so the same value can authenticate the read-only
mirror — and because the token lives in the sidecar env/cache (not only the AKS
deployment), the check still works while the cluster is stopped.

## User-facing change

A new **opt-in, default-OFF** gate `ALLOW_OPENAPI_TOKEN_AUTH` (charter §12a
Rule 4):

- **OFF (default)** — unchanged: the two routes require the MSAL bearer. The
  `X-ELB-API-Token` header is ignored entirely.
- **ON** — the two routes ALSO accept a valid `X-ELB-API-Token`:
  - The header is compared **constant-time** (`hmac.compare_digest`) against the
    authoritative token resolved from `ELB_OPENAPI_API_TOKEN` env → Redis cache.
  - A present-but-wrong token (or a token the server does not know) returns
    **401** — it never silently falls back to MSAL (which would mask a bad token
    as a confusing "missing bearer" error).
  - No header → the standard MSAL bearer path runs (so the SPA, which only sends
    the bearer, is unaffected).

**Deliberately limited to the two read-only routes.** The shared token has no
Azure RBAC gate, so it must never reach a cost-bearing or mutating action;
`POST /api/aks/openapi/ensure-running` (which starts the cluster) stays
MSAL-only.

## Security analysis

- **constant-time compare** prevents a timing oracle on the token.
- **empty authoritative token ⇒ reject** (never bypass): if the server knows no
  token (env + cache both empty), a token-bearing request is 401, so a missing
  secret can't be turned into an open door.
- **default-OFF**: deploying this change does not alter behaviour until an
  operator flips `ALLOW_OPENAPI_TOKEN_AUTH=true`.
- **synthetic identity** for a token-authed request carries a clearly non-UUID
  `object_id` (`openapi-token-caller`) and an empty `raw_token`, so any code that
  mistakes it for an Azure AD principal fails loudly instead of leaking. It only
  ever reaches the two read-only routes (which do not use `caller` for any
  Azure call).
- token value is never logged (only a generic rejection warning).
- Persona Matrix (`api/tests/test_persona_matrix.py`) stays green — the gate is
  additive and default-OFF, so owner/contributor/reader/dev-bypass behaviour is
  unchanged.

## API / IaC diff summary

- `api/auth.py` — new `require_caller_or_openapi_token` dependency plus helpers
  `_openapi_token_auth_enabled`, `_resolve_expected_openapi_token`,
  `_openapi_token_identity`, `is_openapi_token_caller`, and the
  `OPENAPI_TOKEN_OID` sentinel. `require_caller` and the MSAL path are unchanged.
- `api/routes/aks/openapi_databases.py` — the two GET routes switch from
  `Depends(require_caller)` to `Depends(require_caller_or_openapi_token)`;
  docstring updated.
- `infra/control-plane-env.json` — `"ALLOW_OPENAPI_TOKEN_AUTH": "false"` under
  `api` (so `quick-deploy.sh`, which iterates this section, applies it on every
  deploy; the repo default stays OFF).
- `infra/modules/containerAppControl.bicep` — the api sidecar gets the
  `ALLOW_OPENAPI_TOKEN_AUTH` env wired from `controlPlaneEnv.api`.
- `docs/operate/feature-gates.md` — gate documented.
- The compiled ARM templates (`infra/main.json`,
  `infra/modules/containerAppControl.json`) were **not** regenerated: azd
  (`provider: bicep`) and `postprovision.sh` (`--template-file …
  containerAppControl.bicep`) both compile the `.bicep` directly, so those
  checked-in `.json` artifacts are not on the deploy path (they already carried a
  pre-existing `SERVICEBUS_*` drift). Leaving them untouched keeps this change
  scoped to the gate.

## Validation evidence

- `uv run pytest -q api/tests/test_aks_openapi_databases.py` — 26 passed
  (incl. token correct/wrong/empty-expected/gate-off/no-header + helper units).
- `uv run pytest -q api/tests/test_persona_matrix.py api/tests/test_smoke.py` —
  132 passed (no auth regression).
- Full suite on a **clean HEAD worktree** with only these changes overlaid —
  3739 passed, 3 skipped (the 14 failures seen in the dirty working tree are an
  unrelated in-progress `openapi-rebuild` change — `NameError:
  _deploy_failure_is_upstream_reach` in `api/routes/aks/openapi.py` — not caused
  by this work).
- `uv run ruff check` on the changed files — clean.
- `az bicep build` of `containerAppControl.bicep` + `main.bicep` succeeds with
  the new env (artifacts then reverted, see above).
- Enabling the gate live requires a redeploy with
  `ALLOW_OPENAPI_TOKEN_AUTH=true`; to be verified in a follow-up when an operator
  opts in.
