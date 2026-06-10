# Recent searches now discovers directly-submitted `/v1/jobs` jobs across clusters

## Motivation

A BLAST job submitted directly through the elb-openapi sibling service
(`POST /v1/jobs`) did not appear on the **Recent searches** page. The history
view lists jobs subscription-scoped only (no `cluster_name`) so it can show
jobs across every cluster, but the external-job discovery in
`blast_jobs_list` called `_openapi_client_kwargs_from_cluster(subscription_id,
"", "")`, which requires the full `(subscription, resource_group, cluster)`
triple and therefore resolved to `{}`. With empty kwargs the OpenAPI `/v1/jobs`
listing could only be reached through the fragile `ELB_OPENAPI_BASE_URL` env /
runtime-cache fallback — never reliably populated (public-TLS deployments never
call `save_openapi_base_url`, a fresh worker/api process has an empty runtime
cache, and a single global cache key only remembers one cluster). So
directly-submitted jobs stayed invisible until a per-cluster card view happened
to discover and sync them.

## User-facing change

Opening **Recent searches** now lists jobs submitted directly via the OpenAPI
`/v1/jobs` endpoint, on any ElasticBLAST cluster in the subscription, without
having to first visit a per-cluster card. A single Stopped/unreachable cluster
no longer hides jobs that ran on the other reachable clusters; the list is only
flagged `external_degraded` when **every** discovered cluster is unreachable.

## API / IaC diff summary

- `api/services/blast/external_jobs.py`
  - New `_discover_subscription_clusters(subscription_id)` — one cached
    (60 s TTL) ARM `managedClusters.list` round trip returning
    `(resource_group, cluster_name)` pairs for ElasticBLAST clusters. Never
    raises; discovery failures return `[]` so the caller degrades to the env /
    runtime-cache fallback. **Stopped clusters are excluded** (via
    `_cluster_power_state_allows_openapi`) so the ~14 s-polled Recent searches
    endpoint never burns a 10 s `k8s_get_service_ip` timeout per Stopped
    cluster — a Stopped cluster cannot serve `/v1/jobs` anyway, and anything it
    ran while Running is already a local Table row.
  - `_reset_external_jobs_cache()` now also clears the new cluster cache.
- `api/routes/blast/jobs.py`
  - New `_resolve_external_list_targets(...)` resolves a list of OpenAPI
    endpoints to query: a single target for a `cluster_name`-scoped request
    (unchanged), one target per discovered cluster for a subscription-only
    request, or the legacy `{}` env / runtime-cache fallback when nothing
    resolves.
  - `blast_jobs_list` now loops over the resolved targets, de-duplicates jobs
    by `job_id`, applies the per-target scope to each external row, syncs the
    union into Table Storage, and surfaces `external_degraded` only when no
    target was reachable (partial success is not flagged).
- No IaC change.

## Validation evidence

- `uv run pytest -q api/tests` → 3199 passed, 3 skipped.
- New tests in `api/tests/test_external_blast_api.py`:
  - `test_canonical_jobs_list_subscription_scope_discovers_clusters`
  - `test_canonical_jobs_list_subscription_scope_partial_cluster_failure`
  - `test_canonical_jobs_list_subscription_scope_all_clusters_down`
  - `test_discover_subscription_clusters_skips_stopped`
- `uv run ruff check api/routes/blast/jobs.py api/services/blast/external_jobs.py` → clean.
