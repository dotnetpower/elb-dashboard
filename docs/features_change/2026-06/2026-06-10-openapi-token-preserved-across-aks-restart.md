---
title: Preserve the OpenAPI API token across AKS stop/start
description: The elb-openapi API token is now read from the live cluster deployment before minting, so it only rotates on an explicit Generate.
tags:
  - blast
  - operate
---

# Preserve the OpenAPI API token across AKS stop/start

## Motivation

The `elb-openapi` API token (`X-ELB-API-Token`) was changing on **every AKS
restart**. When `az aks stop` → `start` runs, `start_aks` enqueues
`deploy_openapi_service`, which resolved the token from the api-sidecar
`os.environ` and the in-revision Redis cache only. Both are **ephemeral** —
after any control-plane revision restart they are empty — so the deploy fell
through to `secrets.token_urlsafe(32)` and **minted a fresh token**, silently
patching the deployment with a new value. Any external caller that had captured
the previous token started getting `401`.

The durable source of truth for the token is the `ELB_OPENAPI_API_TOKEN` env on
the live `elb-openapi` Deployment, which survives `az aks stop/start` in etcd.
The deploy path simply never read it back.

## User-facing change

- The OpenAPI API token now stays stable across AKS stop/start (and across
  control-plane revision restarts). It only changes when the operator clicks
  **Generate new token** in the SPA API Reference panel (`POST
  /api/aks/openapi/token`) — exactly the explicit-rotation behaviour the user
  expected.
- No SPA change. The API Reference panel and direct callers keep working with
  the same token after a restart.

## API / task diff summary

- `api/services/openapi/token.py`: new public best-effort reader
  `read_cluster_openapi_token(credential, *, subscription_id, resource_group,
  cluster_name, namespace="default")` — returns the live deployment token, or
  `""` when the deployment is absent / has no token env / any K8s error occurs.
  Never mints, never raises.
- `resync_openapi_api_token_from_cluster()` now delegates its cluster read to
  `read_cluster_openapi_token` (DRY; behaviour unchanged).
- `api/tasks/openapi/deploy.py`: token resolution is now cross-cluster-safe.
  Order is `per-cluster runtime_cache → cluster_existing → auto_generated`. The
  process-global `os.environ["ELB_OPENAPI_API_TOKEN"]` and the legacy global
  Redis key are deliberately NOT consulted as deploy-time priority sources —
  one Container App revision can manage several clusters and those globals hold
  the most-recently-touched cluster's token, so reading them would let cluster
  A's deploy stamp cluster B with A's token. When the live deployment already
  carries a token the deploy reuses it, reseeds `os.environ` + the runtime
  Redis cache, and tags `openapi_deploy.api_token_source = "cluster_existing"`.
  The mint path is only reached on a genuine first deploy of THAT cluster.
- `api/services/openapi/runtime.py`: the API token Redis cache is now keyed
  per-cluster, mirroring the existing public-base-url keying.
  `save_openapi_api_token` writes both the legacy global
  `openapi:runtime:api-token` key and a per-cluster
  `openapi:runtime:api-token:cluster:<sha256[:16]>` key when `metadata` carries
  the cluster identity. `get_openapi_api_token(*, subscription_id,
  resource_group, cluster_name)` reads the per-cluster key first and falls back
  to the global key (legacy tokens / context-less readers). Context-less
  callers (`external_blast`, `openapi_proxy`, `external_jobs`) are unchanged —
  they pair with the global base-url and keep reading the global token key.
- No Bicep / Container App layout change. No SAS, no new dependency.

## Validation evidence

- `uv run pytest -q api/tests/test_openapi_token.py` — **15 passed** (4 new
  tests for `read_cluster_openapi_token`: live token, missing context, no token
  env, K8s session error).
- `uv run pytest -q api/tests/test_openapi_runtime_token_cache.py` — **7 passed**
  (per-cluster isolation: writing A then B, each cluster context reads its own
  token while the global key holds B; global fallback for legacy tokens;
  deterministic case-insensitive key; empty-token no-op).
- `uv run pytest -q api/tests` — **3251 passed, 3 skipped** (full backend
  suite; consumer suites `test_openapi_proxy_route`, `test_external_blast_api`,
  `test_azure_tasks` green).
- `uv run ruff check` on every touched file → clean.
