---
title: Two security hardenings — OpenAPI proxy exact-path allowlist and inspector body secret masking
description: The OpenAPI Try-It proxy now treats no-slash allowlist entries as exact paths (not prefixes), and captured request/response bodies in the request inspector mask bearer tokens / SAS signatures / keys before display.
tags:
  - security
  - operate
---

# OpenAPI proxy exact-path allowlist + inspector body secret masking (2026-06-07)

Two independent defence-in-depth fixes found during an E2E security audit.

## Motivation

### 1. OpenAPI Try-It proxy: exact-path entries were prefix-matched

`api/routes/aks/openapi_proxy.py` auto-injects the admin `X-ELB-API-Token`
on every proxied call, so the `_enforce_openapi_proxy_target_path` allowlist
is the gate that stops a tenant member riding that token into a non-public
upstream route. The allowlist documents two entry kinds: an entry ending in
`/` is a **prefix** (`/v1/`, `/docs/`), and an entry without `/` is an
**exact path** (`/healthz`, `/openapi.json`).

The matcher applied `lowered.startswith(prefix)` to **every** entry, so the
exact-path entries were silently treated as prefixes: `/healthzXXX`,
`/healthz/secret`, `/openapi.jsonXXX`, `/openapi.json/dump` all passed the
allowlist and were forwarded upstream with the admin token. The deny-list
still blocked `/admin`, `/internal`, `/debug`, `/private`, `/sudo`, but any
other upstream route sharing a `/healthz` or `/openapi.json` prefix became
reachable — a violation of the documented exact-path contract and an
unnecessary widening of the token-bearing surface.

### 2. Request inspector: captured bodies were not secret-masked

`api/services/request_metrics.py::capture_body` decoded request/response
bodies for the SPA's request-detail inspector feed but, unlike
`redact_headers`, applied no secret masking to the **body**. With full body
capture enabled (`REQUEST_DETAIL_CAPTURE_ENABLED=true`, an operator debug
action), a captured body could carry a bearer token, a SAS signature, an
account/access key, a client secret, a connection string, or a password
(e.g. the OpenAPI proxy whose upstream may echo the injected admin token in
an error) verbatim into the inspector UI — contrary to charter §12
("never echo tokens … or full SAS URLs").

## User-facing change

No change to legitimate flows. The SPA's API Reference Try-It calls
(`/healthz`, `/openapi.json`, `/v1/*`, `/docs/*`) keep working; only
prefix-abuse variants of the exact entries are now rejected with the
existing `openapi_path_not_allowlisted` 400. Inspector bodies now show
`Bearer <redacted>` / `?<sas-redacted>` etc. in place of real secrets.

## API / IaC diff summary

- `api/routes/aks/openapi_proxy.py` — `_enforce_openapi_proxy_target_path`:
  the final allowlist loop now branches on `prefix.endswith("/")`. Prefix
  entries allow the bare root or anything beneath; exact entries
  (`/healthz`, `/openapi.json`) require an exact match. Deny-list and
  traversal/control-char guards are unchanged and still run first.
- `api/services/request_metrics.py` — `capture_body` applies
  `sanitise(text, mask_subscription_ids=False)` after decode. Subscription/
  tenant GUIDs are intentionally preserved (the dashboard surfaces the
  caller's own subscription throughout, and masking them would destroy the
  inspector's debug utility); only genuinely sensitive token/key/SAS/secret
  shapes are masked. Body capture remains default-OFF.
- No IaC change. No RBAC change (charter §12a: no role narrowed; persona
  matrix unaffected; no `require_caller` added to any SSE stream).

## Validation evidence

- `uv run pytest -q api/tests/test_openapi_proxy_route.py` — 31 passed,
  including the new parametrized
  `test_openapi_proxy_exact_entries_are_not_prefix_matched`
  (`/healthzXXX`, `/healthz/secret`, `/healthz-internal`, `/openapi.jsonXXX`,
  `/openapi.json/dump` all rejected) while `/healthz`, `/openapi.json`,
  `/v1/*`, `/docs/*` still allowed.
- `uv run pytest -q api/tests/test_request_metrics_detail.py` — 12 passed,
  including the new `test_capture_body_masks_secrets_but_keeps_subscription_ids`.
- Full `uv run pytest -q api/tests` + persona matrix green; `ruff` clean.
- Live: the OpenAPI proxy allowlist + traversal guards were exercised on the
  running cluster (`/openapi.json` 200; `/`, `/status`, `/secret/keys`,
  `/../admin` all 400) during the same audit.
