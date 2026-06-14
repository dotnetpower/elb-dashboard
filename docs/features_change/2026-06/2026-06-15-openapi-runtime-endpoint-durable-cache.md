# Durable backing for the IP-based OpenAPI runtime endpoint cache

## Motivation

After a Container App revision restart (every deploy), the in-revision Redis
sidecar — where the resolved OpenAPI (sibling `elb-openapi`) endpoint is cached
under `openapi:runtime:base-url` — is wiped. For an IP-based cluster (no public
TLS domain configured) the only way to re-learn the endpoint was a live
`k8s_get_service_ip` resolution on the next list-poll, or the 120 s
public-HTTPS reconciler tick. During that window the dashboard logged repeated
`external blast job list unavailable: openapi_not_configured` and external-job
features (Recent searches sync, Message Flow, failed-job error recovery) were
degraded.

The public-HTTPS endpoint path already persists durably in the
`dashboardsingletons` Storage Table (which survives revision restarts); the
IP-based path was Redis-only. This change extends the same proven durable
pattern to the IP path.

## User-facing change

No new UI. Internal robustness: immediately after a deploy, external-job
features keep working against the last-known endpoint (cluster still Running)
instead of degrading for a poll/reconcile window.

## API / IaC diff summary

No API or IaC change. One service module + one new test file:

- `api/services/openapi/runtime.py`
  - `save_openapi_base_url` now mirrors the endpoint into the durable
    `dashboardsingletons` Table (best-effort) in addition to ops Redis.
  - `get_openapi_base_url` adds a cold-path read: on a Redis miss it
    rehydrates from the durable copy and re-populates Redis, **gated by a
    freshness TTL** (`OPENAPI_RUNTIME_ENDPOINT_MAX_AGE_SECONDS`, default
    `3600`).
  - New helpers `_runtime_endpoint_max_age`, `_payload_age_seconds`,
    `_rehydrate_runtime_base_url_from_durable`.

## Staleness guard (design rationale)

A durably-cached IP could be unreachable if the cluster is **Stopped** (the
sibling pod is down). To bound that:

- The cold-path read only serves the durable endpoint when it is **fresh**
  (`updated_at` within the max-age). A cluster Stopped longer than the window
  no longer serves its stale IP → caller degrades to `openapi_not_configured`
  exactly as before this change.
- A missing/unparseable `updated_at` is treated as **not fresh** (fail-closed).
- Setting `OPENAPI_RUNTIME_ENDPOINT_MAX_AGE_SECONDS` to `0`/negative disables
  the cold-path read entirely, restoring the exact pre-durable behaviour.
- The hot Redis path is unchanged and is the common case; the durable read is
  only paid on a Redis miss (right after a restart). A Redis hit never touches
  the durable store.

Within the freshness window a freshly-Stopped cluster may incur one quick
failed connection per negative-cache window (~30 s) — accepted as a bounded
tradeoff for the far-more-common Running-after-restart availability win. An
AKS LoadBalancer with no healthy backends rejects connections fast (no long
hang).

## Validation evidence

- New tests (all green) in
  `api/tests/test_openapi_runtime_endpoint_durable.py`:
  `test_save_writes_redis_and_durable`,
  `test_redis_hit_does_not_touch_durable`,
  `test_cold_read_rehydrates_from_durable_when_fresh`,
  `test_cold_read_ignores_stale_durable`,
  `test_cold_read_ignores_undatable_durable`,
  `test_cold_read_disabled_when_max_age_non_positive`,
  `test_durable_read_failure_degrades_to_empty`,
  `test_max_age_override_falls_back_to_default`.
- `uv run ruff check api/services/openapi/runtime.py` — clean.
- `uv run pytest -q api/tests` — 3633 passed, 3 skipped (clean env).
- Consumer suites green: `test_external_blast_cluster_resolver`,
  `test_external_blast_api`, `test_openapi_public_https`,
  `test_openapi_runtime_token_cache`.

## Context

Discovered while deploying the external failed-job error recovery fixes
(`docs/features_change/2026-06/2026-06-14-external-failed-job-error-recovery.md`):
a post-deploy revision restart wiped the endpoint cache and, with
`elb-cluster-01` then auto-stopped, surfaced the `openapi_not_configured`
degradation. This change shortens that window for the Running case; the
Stopped case still degrades gracefully (now bounded by the freshness TTL).
