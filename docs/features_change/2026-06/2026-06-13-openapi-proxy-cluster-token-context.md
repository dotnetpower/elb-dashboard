---
title: Thread cluster context into the OpenAPI proxy token lookup
description: >-
  The API Reference "Try it" reverse proxy now resolves the elb-openapi admin
  token with the request's cluster context, so a multi-cluster dashboard reads
  the per-cluster token key instead of the globally most-recently-written one.
tags:
  - blast
  - security
---

# Thread cluster context into the OpenAPI proxy token lookup

## Motivation

Issue [#26](https://github.com/dotnetpower/elb-dashboard/issues/26) tracks the
**outbound** elb-openapi resolver reading the base URL and API token from
process-global, single-key caches. In a single Container App revision that
manages more than one AKS cluster (each with its own custom domain + token),
those globals hold the most-recently-touched cluster's values, so an outbound
call made while the user is on cluster A can be sent with cluster B's token —
confusing 401s or cross-cluster exposure once two clusters each have a custom
domain.

The storage side is already per-cluster keyed (`save_openapi_api_token` /
`get_openapi_api_token` write/read `openapi:runtime:api-token:cluster:<sha>`),
and `get_openapi_api_token` / `get_public_tls_base_url` already accept cluster
context. The gap is **call sites that have cluster context but do not thread
it**.

## User-facing change

- The API Reference "Try it" reverse proxy (`GET /api/aks/openapi/proxy`,
  `aks_openapi_proxy`) resolved its runtime-token fallback via
  `get_openapi_api_token()` with **no cluster context**, even though it already
  passes `subscription_id` / `resource_group` / `cluster_name` to
  `get_public_tls_base_url` and `get_openapi_api_token_status` in the same
  handler. It now passes that same context to `get_openapi_api_token(...)`, so a
  multi-cluster dashboard injects **this cluster's** admin token rather than the
  globally most-recently-written one.
- Single-cluster behaviour is unchanged: the per-cluster key falls back to the
  legacy global key on a miss, and a context-less env token still wins first.

## API / IaC diff summary

- [api/routes/aks/openapi_proxy.py](../../../api/routes/aks/openapi_proxy.py)
  `aks_openapi_proxy` — `get_openapi_api_token()` → `get_openapi_api_token(
  subscription_id=sub, resource_group=resource_group, cluster_name=cluster_name)`.
- No IaC change.

## Scope notes (remaining #26 surface — deferred)

This is one concrete, low-risk slice where the cluster context is fully in
scope. The rest of #26 stays open and is **not** addressed here because the
context is not readily available at those call sites without a per-cluster
resolver design:

- `api/services/external_blast.py` `_base_url()` / `_headers()` remain the
  context-less global fallback. Callers that *have* cluster context already
  resolve per-cluster and pass `base_url` / `api_token` explicitly
  (`api/services/blast/external_jobs.py`, `api/tasks/servicebus/tasks.py`,
  `api/tasks/blast/reconcile_task.py`). The genuinely context-less callers
  (the direct facade routes, the `get_job` fallbacks in
  `api/routes/blast/jobs.py` / `results.py`) only have a `job_id`, not a
  cluster, so making them cluster-correct needs a job→cluster resolver — the
  larger #26 design item.
- httpx connection reuse for the OpenAPI plane (issue #30 candidate fix #4) is
  also folded into this #26 resolver work, since a single shared client cannot
  span per-cluster base URLs.

## Validation evidence

- `uv run pytest -q api/tests/test_openapi_proxy_route.py` — 33 passed.
  New test: `test_openapi_proxy_threads_cluster_context_into_token_lookup`
  asserts the fallback receives `{subscription_id, resource_group, cluster_name}`.
  Updated `test_openapi_proxy_uses_runtime_token_when_env_token_missing` to a
  `**kwargs` stub.
- `uv run pytest -q api/tests/test_openapi_proxy_route.py
  api/tests/test_route_contracts.py api/tests/test_openapi_rate_limit.py
  api/tests/test_openapi_tls_hook.py` — 49 passed.
- `uv run ruff check` — clean.
