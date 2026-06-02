---
title: API security hardening — batch 2 (error request-id, 405/404, readiness detail, anon client-log, error docs)
description: Echoes request_id in error envelopes, distinguishes 405 from 404 on unknown api routes with an Allow header, slims the readiness body behind STRICT_READINESS_DETAIL, opts into anonymous client-log behind ALLOW_ANONYMOUS_CLIENT_LOG, and documents common error responses in the OpenAPI spec.
tags:
  - security
  - architecture
---

# API security hardening — batch 2

## Motivation

Continuation of the live black-box audit of the deployed control plane
(`ca-elb-dashboard`). Batch 2 closes the next tier of contract / observability
gaps on the public api-sidecar surface:

* **#15 — error envelopes had no correlation id.** A `401`/`404`/`422` body
  carried `detail` only, so a support request could not be tied back to the
  `X-Request-ID` already present in the response header and server log.
* **#7 — unknown `/api/*` paths could not be told apart from wrong-method
  calls.** The catch-all answered a flat `404` for both, so a client hitting a
  real route with the wrong verb got no `Allow` hint.
* **#3 — the readiness probe leaked internal topology.** `/api/health/ready`
  returned the full per-component map (which dependencies exist, the credential
  class name, truncated upstream error strings) to an anonymous caller.
* **#14 — pre-login browser errors were silently dropped.** `/api/client-log`
  required a bearer token, so exactly the MSAL redirect/login failures an
  operator most wants to see could never be reported.
* **#10 — the OpenAPI spec documented only `200`/`422`.** A reader could not
  tell that any authenticated route may answer `401`/`403`, that lookups may
  `404`, or that Azure-backed operations may surface a `5xx`.

## User-facing change

* Error responses now echo `request_id` in the JSON body when the request has a
  correlation id, matching the existing `X-Request-ID` header (`401`, `404`,
  `405`, `422`, `5xx`). Additive — existing `detail` is unchanged.
* Unknown `/api/*` paths now return `404 {"detail":"unknown api route", ...}`;
  a request to a **known** route with an unsupported method returns
  `405 {"detail":"method not allowed", ...}` plus an `Allow` header listing the
  supported verbs. Neither case is ever forwarded to the SPA.
* `/api/health/ready` keeps its full per-component body by default. Behind the
  default-OFF `STRICT_READINESS_DETAIL` gate the body collapses to
  `status` / `version` / `retryable` / `retry_after_seconds` only — enough for a
  load balancer / CI gate, nothing for a recon probe.
* `/api/client-log` stays auth-required by default. Behind the default-OFF
  `ALLOW_ANONYMOUS_CLIENT_LOG` gate it also accepts unauthenticated pre-login
  reports, logged with `caller=anonymous` (size caps + sanitisation unchanged).
* When `ENABLE_DOCS=true`, the generated OpenAPI spec now declares a shared
  `ErrorResponse` schema and `401`/`403`/`404`/`500` responses on every
  operation. Documentation only — runtime behaviour is unchanged.

## API / IaC diff summary

| Area | Change |
| --- | --- |
| `api/main.py` | `http_exc_handler` / `validation_handler` set `request_id` in the body via `setdefault`; new `_document_common_error_responses()` injects the shared `ErrorResponse` schema + common error responses into the custom OpenAPI. |
| `api/routes/frontend_proxy.py` | New `_allowed_methods_for_known_path()` (Starlette `Match.PARTIAL`) drives 405-vs-404 on unknown `/api/*` paths; both envelopes carry `request_id`. |
| `api/routes/health.py` | `readiness()` slims the body when `STRICT_READINESS_DETAIL=true` (default OFF). |
| `api/routes/client_log.py` | New `_client_log_caller` optional-auth dependency gated by `ALLOW_ANONYMOUS_CLIENT_LOG` (default OFF); anonymous reports logged as `caller=anonymous`. |
| IaC | None. The new gates default OFF and require no Container App env wiring to preserve current behaviour. |

## Validation evidence

* `uv run pytest -q api/tests/test_api_hardening_batch2.py` — 9 passed
  (request-id envelopes, 405+Allow, 404 unknown route, readiness gate ON/OFF,
  client-log auth ON/OFF, OpenAPI error responses).
* `uv run pytest -q api/tests` — **2404 passed, 3 skipped** (full sweep,
  including `test_persona_matrix.py` and `test_smoke.py` auth-required matrix
  which still expects `POST /api/client-log` → 401 with the gate OFF).
* `uv run ruff check api` — All checks passed.

## Hardening discipline (§12a)

- [x] In scope: `auth` (client-log opt-in), `sanitise` (envelope shaping)
- [x] RBAC change is single-PR safe (no role narrowed) — no role assignments touched
- [x] Persona Matrix tests pass for owner / contributor / reader / dev_bypass
- [x] Reader allowlist unchanged
- [x] Capability Probe passes locally (no Azure surface touched; N/A code paths)
- [x] RBAC removal preflight green locally — no `roleAssignments` diff in this change
- [x] New guards ship default-OFF behind `STRICT_READINESS_DETAIL` /
  `ALLOW_ANONYMOUS_CLIENT_LOG` env vars (`#10`/`#7`/`#15` are additive docs/contract
  changes, not auth tightening, so they ship always-on)
- [x] No `Depends(require_caller)` added to an SSE event stream
- [x] Change note (this file) summarises persona impact

Persona impact: all four personas keep their current access. The two new gates
default OFF, so the deployed behaviour is byte-identical until an operator opts
in; `ALLOW_ANONYMOUS_CLIENT_LOG=true` is the only gate that *relaxes* auth and is
intentionally opt-in.
