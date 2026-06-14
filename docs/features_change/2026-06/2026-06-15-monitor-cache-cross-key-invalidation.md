---
title: monitor snapshot cache — cross-key invalidation no longer stalls background refresh
description: Fixed a concurrency/liveness defect where invalidating one monitor cache key could leave an unrelated key's background-refresh flag stuck, blocking its refresh until the stale window expired.
tags:
  - operate
  - architecture
---

# 2026-06-15 — monitor cache cross-key invalidation liveness fix

## Motivation

A self-critique pass over the monitor snapshot cache's concurrency model
surfaced a latent liveness defect. The cache uses a **global** `_GENERATION`
counter to cancel an in-flight background refresh whose result became stale due
to a cache invalidation (e.g. an AKS start/stop mutation calls
`invalidate_monitor_snapshot_prefix`). But `invalidate_monitor_snapshot_prefix`
only **removes the matched keys** while bumping the global counter.

So when key **X** (e.g. `rg-A`) is invalidated while an **unrelated** key **Y**
(e.g. `rg-B`) has a background refresh in flight, Y's refresh sees the bumped
generation and is discarded — correct so far — but **Y's entry stays in the
cache with its `refreshing` flag stuck `True`**. That flag is what gates whether
a stale read may start a new background refresh, so every subsequent poll of Y
served stale without ever re-refreshing, until Y's stale window expired (up to
`_MAX_STALE_SECONDS` = 3600 s) and a synchronous refresh finally healed it.

## User-facing change

No API/UI surface change. A dashboard tile backed by monitor key Y now
refreshes on its normal cadence even when an unrelated key X was invalidated
mid-refresh, instead of being pinned to stale data for minutes.

## Internal diff summary

* `api/services/monitor_cache.py` — `_refresh` success path: on a generation
  mismatch (the result is discarded) it now clears the `refreshing` flag on
  whatever entry is currently cached for that key, so the next poll can
  re-trigger a background refresh immediately. Previously the mismatch branch
  did nothing, leaving the flag stuck.

## Validation evidence

* New regression test
  `test_monitor_cache.py::test_cross_key_invalidation_does_not_stick_unrelated_refreshing_flag`:
  seeds key Y, makes it stale (flag set `True`), invalidates an unrelated key X
  (bumps generation, leaves Y in cache), runs Y's in-flight refresh, and asserts
  (a) Y's `refreshing` flag is cleared and (b) the next stale poll of Y queues a
  fresh background refresh. Fails without the fix (flag stays `True`).
* `uv run ruff check api` — clean.
* `uv run pytest -q api/tests` — 3624 passed, 3 skipped.
