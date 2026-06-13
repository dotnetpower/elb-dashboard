---
title: Warm BLAST DB catalogue cache in the api process
description: Hide the cold catalogue enumeration from the first dashboard load by
  warming the process-local cache in the api sidecar ahead of its TTL.
tags:
  - blast
  - operate
---

# Warm BLAST DB catalogue cache in the api process

## Motivation

`GET /api/blast/databases` enumerates the `blast-db` container (full blob list +
per-DB metadata reads), which costs ~4 s when cold. The api already has a
process-local single-flight + TTL cache
(`api/services/storage/database_catalog_cache.py`, default TTL 300 s), but once
the TTL expires the very next user pays the full cold enumeration. This is part
of the dashboard first-paint latency work (thundering-herd on the single api
sidecar).

A first attempt added a Celery **beat** task to warm the cache. Self-critique
caught a High design defect before it shipped: the catalogue cache is
**process-local** and only *invalidation* is fanned out across sidecars (via the
`db_metadata` Redis channel) — a *fill* is not. The beat task runs in the
**worker** process, so it would warm the worker's cache while the **api** process
(which serves the read path) stays cold. The beat approach was a no-op for the
stated goal and was reverted.

## User-facing change

The api process now keeps the catalogue cache hot itself. A single background
loop in the app lifespan warms the cache at startup and then every
`BLAST_DB_CATALOG_WARM_SECONDS` (default **240 s**, slightly ahead of the 300 s
cache TTL). The first New Search / Database Builder load after an idle period no
longer waits on the cold ~4 s enumeration; the user sees the cached catalogue
immediately. No new UI, no API contract change.

Behaviour details:

- Account is resolved from `STORAGE_ACCOUNT_NAME`, then `AZURE_STORAGE_ACCOUNT`.
  With neither set (local dev without a workload Storage account) the warmer does
  not start — it is a no-op.
- The warm tick uses `list_databases_cached` **without** `force_refresh`, so a
  still-fresh cache or a concurrent single-flight fill from a real request is
  reused; back-to-back ticks touch Storage at most once.
- Every tick is read-only and never raises — a failed tick degrades and the loop
  keeps ticking. Set `BLAST_DB_CATALOG_WARM_SECONDS=0` to disable.

## API / IaC diff summary

- **API**: no route or response change. New module
  `api/services/storage/catalog_warmer.py`; lifespan start/stop hooks in
  `api/app/lifespan.py`.
- **IaC**: none. The interval is an optional code-default env override
  (`BLAST_DB_CATALOG_WARM_SECONDS`, default 240) and is not a security guard, so
  it is not added to `infra/control-plane-env.json`.

## Validation evidence

- `uv run ruff check api/services/storage/catalog_warmer.py api/app/lifespan.py api/tests/test_catalog_warmer.py` → All checks passed.
- `uv run pytest -q api/tests/test_catalog_warmer.py` → 10 passed (account
  precedence, no-op without account, no `force_refresh`, no-raise degrade,
  interval resolution, start/stop lifecycle + idempotent start + at-least-one
  tick).
- `uv run pytest -q api/tests/test_catalog_warmer.py api/tests/test_lifespan_threadpool.py api/tests/test_smoke.py` → 92 passed (lifespan integration + app startup with the warmer wired in).
