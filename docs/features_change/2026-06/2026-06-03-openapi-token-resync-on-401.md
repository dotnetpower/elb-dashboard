---
title: Reactive OpenAPI token resync on 401 readiness probe
description: Self-heal a stale dashboard OpenAPI token by re-reading it from the elb-openapi deployment when /v1/ready returns 401, so BLAST submission stops failing after a control-plane redeploy.
tags:
  - blast
  - operate
---

# Reactive OpenAPI token resync on 401 readiness probe

## Motivation

After AKS starts and a BLAST job is submitted, the UI showed:

> Submission failed: elb-openapi readiness probe failed (openapi_http_401)

Root cause confirmed from live Log Analytics (sibling `/v1/ready` and
`/v1/jobs` answering HTTP 401 from the in-cluster ILB): the dashboard's
runtime OpenAPI token cache had gone stale.

The OpenAPI API token is minted at elb-openapi deploy time and stored in two
places — the pod deployment env `ELB_OPENAPI_API_TOKEN` and the dashboard's
**ephemeral** ops Redis sidecar (`openapi:runtime:api-token`). The api sidecar
has no matching env var, so it depends entirely on Redis. A control-plane
redeploy (`quick-deploy.sh all`) or revision restart wipes that ephemeral
Redis while the elb-openapi pod keeps its token, so the dashboard sends an
empty / stale `X-ELB-API-Token` and the sibling rejects every call with 401.
AKS stop/start does not change the pod's token, so only the dashboard's cache
is out of sync.

## User-facing change

The readiness gate now **self-heals** the stale token. When `/v1/ready`
returns 401, the gate re-reads the live `ELB_OPENAPI_API_TOKEN` from the
elb-openapi deployment env (using the AKS cluster context cached alongside the
OpenAPI base URL), syncs it back into the runtime token cache, and retries the
probe once. The 401 turns into a normal ready payload with no operator action,
and because the resync writes to the cache, every subsequent sibling call
(submit, job list) automatically uses the fresh token.

The recovery **never mints** a token — a 401 means the pod already holds a
valid token the dashboard simply lost, so re-reading and re-syncing is the
correct, side-effect-free fix. If recovery is not possible (no cached cluster
context, RBAC failure, or the pod genuinely has no token env entry) the
original 401 surfaces unchanged and the retry does not loop (the single retry
runs with resync disabled).

## API / IaC diff summary

No HTTP contract change, no IaC change. Internal service edits only:

- `api/services/openapi/runtime.py` — new `get_openapi_runtime_metadata()`
  reads the metadata (`subscription_id` / `resource_group` / `cluster_name`)
  stored alongside the cached OpenAPI base URL.
- `api/services/openapi/token.py` — new
  `resync_openapi_api_token_from_cluster()` re-reads the live deployment
  token and syncs it into the runtime cache (best-effort; never mints).
- `api/services/external_blast.py` — `_ready_probe_upstream()` gains
  `allow_token_resync` (default `True`); on a 401 it resyncs and retries once
  with `allow_token_resync=False`.

This is a bug fix that restores intended behaviour, not a §12a security
hardening gate, so it ships ON (no `STRICT_*` / `ENFORCE_*` env gate).

## Validation evidence

- `uv run pytest -q api/tests/test_openapi_token.py` — 11 passed (3 new resync
  tests: reads pod token + syncs without minting; skips without cluster
  context; returns "" when the pod has no token env entry).
- `uv run pytest -q api/tests/test_external_blast_api.py -k ready` — 13 passed
  (2 new tests: 401 → resync → retry success; 401 with no recovery surfaces
  the error and fires no retry loop).
- `uv run pytest -q api/tests` — 2446 passed, 3 skipped. The single
  `test_security_headers.py::test_openapi_hidden_by_default` failure under
  `-n auto` is a pre-existing docs env-var ordering flake unrelated to this
  change; it passes in isolation and within its own file.
- `uv run ruff check api` — all checks passed.

## Immediate manual unblock

A deployed environment hitting this 401 before the fix lands can clear it by
opening the dashboard's API menu, which triggers
`get_openapi_api_token_status` → reads the pod env → syncs Redis.
