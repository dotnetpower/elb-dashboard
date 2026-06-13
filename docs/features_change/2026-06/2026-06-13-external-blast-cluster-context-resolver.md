---
title: Per-cluster cluster context for the external-BLAST outbound resolver
description: >-
  `external_blast._base_url` / `_headers` now accept explicit cluster context
  and resolve the per-cluster runtime base URL + API token keys, threaded
  through the submit / get / list entry points, so a multi-cluster revision
  targets the requested cluster instead of the globally most-recent endpoint.
tags:
  - blast
  - security
---

# Per-cluster cluster context for the external-BLAST outbound resolver

## Motivation

Issue [#26](https://github.com/dotnetpower/elb-dashboard/issues/26) tracks the
**outbound** elb-openapi resolver reading the base URL and API token from
process-global, single-key caches. In a single Container App revision that
manages more than one AKS cluster (each with its own custom domain + token),
those globals hold the most-recently-touched cluster's values, so an outbound
call made while the user is on cluster A can be sent to cluster B's domain with
B's token — confusing 401s or cross-cluster exposure once two clusters each have
a custom domain.

The storage side (`save_openapi_*` / `get_openapi_*`), `get_public_tls_base_url`,
and the API Reference proxy (commit `a7d9949`) are already per-cluster keyed.
This change closes the next slice: the `external_blast` client's own resolvers.

## User-facing change

No behaviour change for existing callers — every new parameter defaults to empty
and the global-key resolution is preserved (charter §12a Rule 4: additive /
default-OFF). A caller that supplies the cluster context now resolves
**that cluster's** endpoint and token from the per-cluster cache.

- `external_blast._base_url(value=None, *, subscription_id="", resource_group="",
  cluster_name="")` — when no explicit `value` / env override and the full
  cluster context is supplied, prefers `get_public_tls_base_url(<cluster>)`
  (the per-cluster public HTTPS key); a miss falls through to the legacy global
  runtime key.
- `external_blast._headers(*, api_token=None, internal_token=None,
  subscription_id="", resource_group="", cluster_name="")` — passes the cluster
  context to `get_openapi_api_token(<cluster>)` so the per-cluster token key is
  read first, with the legacy global key as a fallback. The token value is never
  logged.
- `submit_job` / `get_job` / `list_jobs` accept the same three context kwargs
  (defaulted) and thread them into `_base_url` / `_headers`, so an outbound call
  scoped to cluster A resolves A's base URL **and** A's token end-to-end.

## API / IaC diff summary

- [api/services/external_blast.py](../../../api/services/external_blast.py) —
  `_base_url` / `_headers` gain cluster-context kwargs; `submit_job` / `get_job`
  / `list_jobs` thread them through.
- No IaC change. No new dependency.

## Scope notes (remaining #26 surface — deferred)

- The genuinely **context-less** facade routes
  ([api/routes/elastic_blast.py](../../../api/routes/elastic_blast.py),
  the `get_job` fallbacks in
  [api/routes/blast/jobs.py](../../../api/routes/blast/jobs.py) /
  [api/routes/blast/results.py](../../../api/routes/blast/results.py)) only carry
  the sibling's `job_id`, not a cluster, so they still resolve the global key.
  Making them cluster-correct needs a **job→cluster resolver** plus a
  **multi-cluster submit request contract** — the larger #26 design item that
  cannot be safely validated without a live multi-cluster deployment.
- `ready` (the submit pre-flight) and the streaming download/upload proxy are
  left on the global path for now; their cluster-aware token-resync path is a
  follow-up tied to the same resolver work.
- httpx connection-pool reuse for the OpenAPI plane (issue #30 candidate fix #4)
  remains folded into this resolver work, since a single shared client cannot
  span per-cluster base URLs.

## Validation evidence

- `uv run pytest -q api/tests/test_external_blast_cluster_resolver.py` — 8 passed.
  Proves per-cluster base URL + token resolution for clusters A/B, the global
  fallback for a context-less call and an unknown cluster, the explicit-value
  short-circuit, and that `submit_job` / `get_job` / `list_jobs` thread the
  context to the resolvers.
- `uv run pytest -q api/tests/test_external_blast_api.py
  api/tests/test_openapi_proxy_route.py api/tests/test_openapi_runtime_token_cache.py
  api/tests/test_openapi_public_https.py api/tests/test_openapi_tls_hook.py` —
  160 passed.
- `uv run ruff check api/services/external_blast.py
  api/tests/test_external_blast_cluster_resolver.py` — clean.
