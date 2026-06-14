---
title: "OpenAPI webhook receiver: token from ops Redis"
description: "Receiver falls back to the worker-minted token cached in ops Redis when the api sidecar has no shared-secret env var."
tags:
  - blast
  - operate
---

## Motivation

The F4 webhook receiver shipped earlier in the day
([2026-06-14-openapi-webhook-activation.md](2026-06-14-openapi-webhook-activation.md))
expected the api sidecar to carry `ELB_OPENAPI_API_TOKEN` (or
`ELB_OPENAPI_INTERNAL_TOKEN`) in its env so it could compare the bearer the
sibling pod attaches. Live verification on revision
`ca-elb-dashboard--0000454` returned `HTTP 503 webhook_not_configured`
because the api sidecar template only carries `OPENAPI_ALLOW_PUBLIC_LB` and
`ENFORCE_OPENAPI_EXEC_RBAC` — the actual shared secret is minted dynamically
by the worker's `deploy_openapi_service` task and persisted to ops Redis
(`save_openapi_api_token` → key `openapi:runtime:api-token`). The worker
process sets the env var on itself; the api sidecar process never sees it.

## User-facing change

None directly. End-to-end behaviour is unchanged from the F4 plan: when
`deploy_openapi_service` has run at least once for the active cluster, the
receiver now accepts the sibling's webhook and updates the jobstate row
without any operator-supplied env config.

## API / IaC diff summary

* [api/routes/blast/external_webhook.py](../../../api/routes/blast/external_webhook.py)
  — `_expected_token()` now tries env first, then falls back to
  `api.services.openapi.runtime.get_openapi_api_token()` (context-less
  global key read). Failure of the Redis lookup is logged at DEBUG and
  treated as "no token" → 503 (fail-closed).
* [api/tests/test_external_webhook.py](../../../api/tests/test_external_webhook.py)
  — adds `test_register_external_job_accepts_runtime_cache_token` and
  pins the existing 503 test to a stubbed empty cache so it does not
  depend on Redis reachability in CI.

No IaC change.

## Validation evidence

* `uv run ruff check api/routes/blast/external_webhook.py api/tests/test_external_webhook.py` — clean.
* `uv run pytest -q api/tests/test_external_webhook.py` — 16 passed.
* `uv run pytest -q api/tests` — 3547 passed, 3 skipped.
* Post-deploy live probe to be appended after `quick-deploy.sh api` rolls
  the api sidecar and `deploy_openapi_service` runs.
