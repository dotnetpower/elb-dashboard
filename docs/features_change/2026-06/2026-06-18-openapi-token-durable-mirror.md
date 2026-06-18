---
title: Durable-mirror the OpenAPI webhook token across revision restarts
description: Persist the OpenAPI shared-secret in the durable singletons table so an ephemeral-Redis flush no longer 503s the register-external-job webhook.
tags:
  - operate
  - blast
  - security
---

# Durable-mirror the OpenAPI webhook token (#49)

## Motivation

`POST /api/blast/register-external-job` returned `503 webhook_not_configured` in
bulk (131 requests / 7 days; 120 of them in a single 06-17 window). The receiver
fails closed when it cannot resolve the shared webhook token, and the token lived
**only** in the in-revision ops Redis sidecar. A Container App revision restart
wipes that Redis, so every sibling webhook 503'd until an explicit redeploy
re-seeded the token. The reactive 401 self-heal
(`resync_openapi_api_token_from_cluster`) only fires on **outbound** 401s, so it
never covered the **inbound** webhook path.

## Root cause

Unlike the OpenAPI base-url — which is already mirrored into the durable
`dashboardsingletons` Storage Table and rehydrated on a cold read — the API token
had no durable backing. Ephemeral Redis was its only home.

## User-facing change

None visible. The webhook simply stops 503'ing after a revision restart: the
global token is rehydrated from the durable store on the first cold read and
re-populated into Redis, so subsequent webhooks are hot again.

## API / IaC diff summary

- `api/services/openapi/runtime.py`
  - `save_openapi_api_token()` now also mirrors the **global** token payload into
    the durable singletons table (`_durable_save_safe(_TOKEN_KEY, payload)`),
    best-effort — the durable write never fails the call (mirrors
    `save_openapi_base_url`).
  - `get_openapi_api_token()` rehydrates the global token from the durable store
    when both the per-cluster and global Redis reads miss, via the new
    `_rehydrate_token_from_durable()` helper (re-populates Redis, returns `""` on
    durable miss so genuinely-unconfigured stays fail-closed). No freshness gate:
    the durable copy is refreshed on every token write/rotation and any real
    drift is still caught by the 401 self-heal path.
- `api/tests/test_openapi_runtime_token_cache.py` — four new tests: durable
  mirror on save, cold-Redis rehydration, fail-closed on durable miss, and "hot
  Redis read never touches the durable store".

## Security posture

The token is a **rotatable webhook shared-secret**, not a long-lived credential.
The durable copy lives in the same `publicNetworkAccess: Disabled`,
private-endpoint-only Storage account as the base-url and jobstate rows, RBAC-gated
by the shared user-assigned MI — i.e. the **same trust boundary** the token
already crosses in Redis and the AKS pod env (`ELB_OPENAPI_API_TOKEN`). The
singleton store never logs payload values (only key names + exception types), so
the token is not exposed in logs. No SAS token, no browser exposure, no new
network surface. This is not a §12a hardening change (it does not tighten/loosen
auth, RBAC, network, JWT, ticket, CORS, or sanitisation).

## Validation evidence

```
uv run pytest -q api/tests/test_openapi_runtime_token_cache.py api/tests/test_external_webhook.py  # 42 passed
uv run pytest -q api/tests/test_openapi_token.py api/tests/test_openapi_proxy_route.py \
  api/tests/test_external_blast_api.py api/tests/test_state_singletons.py             # 179 passed
uv run pytest -q api/tests                                                            # 3951 passed, 3 skipped
uv run ruff check api/services/openapi/runtime.py api/tests/test_openapi_runtime_token_cache.py  # clean
```
