---
title: API security hardening — batch 1 (headers, banner, token error, spec gating)
description: Adds baseline security response headers, masks the Server banner, sanitizes the token-error envelope, gates /openapi.json behind ENABLE_DOCS, and declares the OpenAPI bearer security scheme.
tags:
  - security
  - architecture
---

# API security hardening — batch 1

## Motivation

A live black-box audit of the deployed control plane (`ca-elb-dashboard`)
surfaced several low-risk information-disclosure and contract gaps on the
public api-sidecar surface:

1. **No security response headers** — responses lacked HSTS,
   `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, and
   `Permissions-Policy`.
2. **`Server` version banner leak** — `Server: uvicorn,nginx/1.31.1` exposed the
   framework and nginx build.
3. **Token error leaked PyJWT internals** — `invalid token: Not enough segments`
   handed an attacker probing token shapes free recon.
4. **`/openapi.json` anonymously public** — the full machine-readable route
   inventory (201 KB) was served to any unauthenticated caller.
5. **OpenAPI spec missing `securitySchemes`** — every route enforces an MSAL
   bearer token at runtime, but the spec advertised no security requirement.

## User-facing change

* Every api-sidecar response now carries `Strict-Transport-Security`,
  `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`,
  and `Permissions-Policy`. The middleware uses `setdefault`, so the
  SPA-tuned headers the frontend nginx sidecar already sets on proxied static
  assets are preserved (no clobbering of its CSP / referrer policy).
* The `Server` response header is masked to a constant `ElasticBLAST`; uvicorn's
  own banner is suppressed via `--no-server-header` and nginx's via
  `server_tokens off`.
* Invalid bearer tokens now return `{"detail": "invalid token"}` — the specific
  PyJWT reason stays in the server log only.
* `/openapi.json` and `/api/docs` are now both gated behind the existing
  `ENABLE_DOCS` flag (unset in production → both hidden). Runtime auth is
  unchanged, so hiding the spec strips no caller's access.
* When the spec **is** enabled, it now declares a `BearerAuth`
  (`http`/`bearer`/`JWT`) security scheme and a global `security` requirement.
* A `Content-Security-Policy` header for the api sidecar is available behind a
  new **default-OFF** `STRICT_CSP` gate (§12a Rule 4); `STRICT_CSP_POLICY`
  overrides the conservative default.

## API / IaC diff summary

| File | Change |
|------|--------|
| `api/app/security_headers.py` | **new** `SecurityHeadersMiddleware` (always-on headers, banner mask, CSP behind `STRICT_CSP`) |
| `api/main.py` | register middleware (outermost); gate `openapi_url` behind `ENABLE_DOCS`; `_install_openapi_security_scheme()` |
| `api/auth.py` | generic `invalid token` client message; PyJWT reason logged only |
| `api/Dockerfile` | add `--no-server-header` to the uvicorn CMD |
| `web/nginx.conf` | add `server_tokens off;` |
| `api/tests/test_security_headers.py` | **new** — 9 tests covering both gate states |

> Dockerfile + nginx.conf changes only take effect on the next image build /
> deploy; they were not deployed as part of this change (charter §13 — validated
> locally via pytest only).

## Validation evidence

* `uv run pytest -q api/tests/test_security_headers.py` → **9 passed**
* `uv run pytest -q api/tests` → **2395 passed, 3 skipped**
* `uv run pytest -q api/tests/test_persona_matrix.py` → green (no authz change)
* `uv run ruff check api` → All checks passed

## Hardening discipline (§12a)

- [x] In scope: sanitise, network (headers), jwt (spec scheme)
- [x] RBAC change is single-PR safe (no role narrowed)
- [x] Persona Matrix tests pass for owner / contributor / reader / dev_bypass
- [x] Reader allowlist unchanged
- [x] Capability Probe N/A (no role change)
- [x] RBAC removal preflight N/A (no `roleAssignments` diff)
- [x] New guard ships default-OFF behind `STRICT_CSP` (CSP header)
- [x] No `Depends(require_caller)` added to an SSE event stream
- [x] This change note summarises persona impact (none — additive headers,
      documentation-only spec changes, generic error string)
