---
title: Bound the live pricing cache so the api sidecar cannot leak memory
description: Add a hard size cap with expired-first eviction to the opt-in Azure Retail Prices cache, closing the only unbounded module-level cache in the api sidecar found during a six-sidecar memory-stability audit.
tags:
  - operate
  - security
---

# Bound the live pricing cache size

## Motivation

A memory-stability audit of the six Container App sidecars (`frontend`, `api`,
`worker`, `beat`, `redis`, `terminal`) confirmed the control plane is already
well hardened against leaks: the Celery `worker` recycles prefork children by
task count (`worker_max_tasks_per_child=200`) and resident memory
(`CELERY_WORKER_MAX_MEMORY_PER_CHILD_KB=250000`), `redis` runs with a
`maxmemory 384mb` guardrail, and every long-lived in-memory store in the `api`
sidecar (JWKS/claims caches, blob-service pool, k8s session pool, rate-limiter
buckets, execution-admission memory, SSE/WebSocket tickets) already carries a
size cap or TTL sweep.

The audit surfaced **one** exception: the live pricing cache in
[api/services/cost/pricing.py](../../../api/services/cost/pricing.py) evicted
only by TTL (24h for a real price, 1h for a miss) and had **no size cap**. The
`(region, SKU)` key cardinality is finite and small in practice, and the whole
module is gated behind `COST_PRICING_LIVE` (default OFF), so the real-world risk
was low — but it was the only structurally unbounded module-level cache in the
sidecar, so a future caller that enumerated many regions/SKUs could grow it
without limit.

## User-facing change

None. Internal hardening only.

## API / IaC diff summary

- [api/services/cost/pricing.py](../../../api/services/cost/pricing.py): added
  `_CACHE_MAX_ENTRIES` (default 512, env-tunable via
  `COST_PRICING_CACHE_MAX_ENTRIES`) and an `_evict_over_cap_locked()` helper
  called from `_cache_put` under the existing `_CACHE_LOCK`. Eviction reclaims
  already-expired entries first (respecting each entry's own TTL), then drops
  the oldest-fetched entries in a single sorted pass. `_cache_put` /
  `_cache_get` / `reset_cache` signatures and return contracts are unchanged.
  Hardened after a 3-round bug/risk critique:
  - **Import safety**: the override is parsed through a defensive `_int_env`
    helper (try/except + clamp) so a malformed `COST_PRICING_CACHE_MAX_ENTRIES`
    can no longer raise `ValueError` at import and take down the whole api
    sidecar; it logs a warning and falls back to the default.
  - **Cap cannot be defeated**: the override is clamped to `[64, 100000]`, so a
    huge value cannot re-open unbounded growth and a tiny one cannot thrash.
  - **Eviction is O(n log n)**: the initial `while len > cap: min(...)` (worst
    case O(n²)) was replaced with a single sorted-slice batch drop.
- [api/tests/test_cost_pricing.py](../../../api/tests/test_cost_pricing.py):
  regression tests — `test_cache_is_size_capped`,
  `test_cache_reclaims_expired_before_evicting_valid`,
  `test_int_env_falls_back_on_malformed`, `test_int_env_clamps_out_of_range`,
  and `test_cache_batch_eviction_keeps_newest`.

## Validation evidence

- `uv run pytest -q api/tests/test_cost_pricing.py` → 11 passed (6 existing + 5 new).
- `uv run pytest -q api/tests/test_cost_estimate.py` → passing (consumer of
  `live_hourly_price_usd`, unaffected by the internal cache change).
- `uv run ruff check api/services/cost/pricing.py api/tests/test_cost_pricing.py`
  → all checks passed.
