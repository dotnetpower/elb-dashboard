---
title: New Search database listing — cached + event-invalidated + prefetched
description: Speed up the New Search "Choose Search Set" step on workspaces with many BLAST databases by caching the blast-db catalogue enumeration, invalidating it on change, and prefetching it from the Dashboard.
tags:
  - blast
  - ui
---

# 2026-06-05 — New Search database listing: cache + event invalidation + prefetch

## Motivation

On a workspace with many BLAST databases, opening **New Search** showed a
loading skeleton on the "Choose Search Set" step for a noticeable beat. The
cost is in `GET /api/blast/databases` → `list_databases()`, which enumerates the
entire `blast-db` Storage container and then reads 1–3 metadata blobs per
database. That is `O(N)` sequential Storage round-trips, paid again on every page
load, on every cluster-topology change, and by each independent consumer
(New Search, Warmup card, Database Builder, Terminal config).

The catalogue changes rarely — only when an admin prepares, deletes, or shards a
database — so it is a natural caching target.

## User-facing change

- New Search opens the database picker instantly on re-visits and after a
  Dashboard visit (the listing is prefetched while the user is on the Dashboard
  and on hover/focus of the **New Search** nav link).
- The list still reflects admin actions immediately: preparing, deleting, or
  sharding a database invalidates the cache so the next read is fresh — no TTL
  wait for in-app changes.

## What changed

### Backend — read-path catalogue cache

- New `api/services/storage/database_catalog_cache.py`:
  `list_databases_cached()` wraps the `storage.data.list_databases` facade with a
  process-local, account-keyed TTL cache (default 300 s,
  `BLAST_DB_CATALOG_CACHE_TTL`), single-flight coordination (mirrors the existing
  `db_metadata` cache), and JSON-bytes storage so every hit returns a fresh
  mutable list (the route's `warmup_plan` enrichment cannot corrupt the entry).
  The degraded no-account path and enumeration failures are never cached.
- `api/routes/blast/databases.py` `blast_databases` now calls
  `list_databases_cached`. The per-request `warmup_plan` enrichment is unchanged
  and runs on top of the cached base list, so a cluster switch reuses the cache
  (hits the catalogue cache, not Storage) instead of re-paying the enumeration.

### Backend — event-based invalidation (correctness)

- `api/services/blast/db_metadata.py`:
  `notify_blast_db_metadata_changed()` and the Redis pub/sub subscriber now also
  drop the account-scoped catalogue listing cache (lazy import, best-effort), so
  every existing call site that already invalidated the display-metadata cache
  (prepare-db start/success/failure, delete) now invalidates the listing too —
  across all sidecars via the shared `elb:cache:blast-db-metadata` channel.
- `api/routes/blast/databases.py` `_do_shard` now calls
  `notify_blast_db_metadata_changed` after a successful shard — closing a
  pre-existing gap where sharding rewrote `{db}-metadata.json` without dropping
  the metadata cache. (The worker warmup auto-shard already published.)

### Frontend — prefetch + cache reuse

- New `web/src/hooks/usePrefetchBlastDatabases.ts`: `usePrefetchBlastDatabases`
  (Dashboard) + `prefetchBlastDatabasesQuery` (imperative, for nav hover). Uses a
  topology-free query key `["blast-databases", sub, account, 0, ""]` that matches
  the page's first render, so New Search mounts to a cache hit.
- `web/src/pages/Dashboard/useGettingStartedReadiness.ts` fires the prefetch
  alongside the existing API Reference prefetch.
- `web/src/components/Layout.tsx` prefetches on hover/focus of the **New Search**
  nav link.
- `web/src/pages/blastSubmit/useDbWithWarmupPlan.ts` pins `staleTime: 120 s` and
  `gcTime: 30 min` so the prefetched/cached listing is reused without re-showing
  the skeleton, and seeds the topology-scoped query with the topology-free base
  listing via `placeholderData` so the picker renders immediately on first
  visit while the `warmup_plan`-enriched rows load in the background (critique
  finding C).

### Hardening from the self-critique pass

- **A — invalidate-races-cold-fill guard.** `database_catalog_cache.py` now keeps
  a per-account epoch counter. A single-flight (or `force_refresh`) leader
  snapshots the epoch before enumerating and only commits to the cache if the
  epoch is unchanged; `invalidate_blast_db_listing_cache` bumps it. An admin
  delete/prepare that lands *during* a cold enumeration therefore cannot be
  overwritten by the leader's pre-change snapshot — the caller still gets the
  snapshot, but it is not pinned for the TTL.
- **B — explicit cache bypass for the admin surface.**
  `list_databases_cached(..., force_refresh=True)` re-enumerates and refreshes
  the shared cache; the route accepts `?fresh=1`; `blastApi.listDatabases` takes
  an `options.fresh` flag; and the Database Builder's existing-DB query (whose
  **Refresh** button is a refetch) now always requests `fresh: true` so the
  admin surface shows the true Storage state and authoritatively refreshes the
  cache New Search reads.

## API / IaC diff summary

No HTTP contract change to the response: `GET /api/blast/databases` response
shape is identical. One additive optional query param (`fresh`, default false).
One additive optional arg on `blastApi.listDatabases` (`options.fresh`). No new
dependencies, no Bicep, no Container App template change. One new optional env
var (`BLAST_DB_CATALOG_CACHE_TTL`, defaulted in code).

## Why no on-disk / sentinel marker

A sentinel marker blob was considered for "cheap change detection" but rejected:
it cannot detect genuine out-of-band changes (terminal `azcopy`, NCBI
auto-refresh) without re-enumerating the container — which defeats its purpose.
In-app changes are already covered immediately by event invalidation; out-of-band
changes are bounded by the short TTL backstop. Adding the marker would be
complexity without payoff.

## Validation

- Backend: `uv run pytest -q api/tests` — 2862 passed, 3 skipped. New
  `api/tests/test_database_catalog_cache.py` (13 cases: cache hit, isolated copy,
  per-account separation, per-account + global invalidation, no-account
  passthrough, failure propagation/not-cached, TTL expiry, invalidate-during-fill
  guard, force_refresh bypass + repopulate, force_refresh-during-invalidation).
  `test_blast_databases_warmup_plan` adds a `fresh=1` route case. Affected suites
  (`test_prepare_db_delete_route`, `test_prepare_db_hardening`,
  `test_storage_data`, `test_auto_warmup`) green.
- Frontend: `npm run build` clean; `npx vitest run` for the new prefetch test
  (4 cases) + the blastSubmit suite (176 tests) green.
- Lint: `uv run ruff check` clean on touched backend files; `eslint` clean on
  touched TS files.
