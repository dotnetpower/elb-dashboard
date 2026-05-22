# Tail hardening: inflight TTL + event_emitter shutdown + blob fast path

## Motivation
Three small but real correctness/perf fixes:

* `_DISPLAY_METADATA_INFLIGHT` had no leader-exit timeout. If the leader
  task crashed before the `finally` (SIGKILL, OOM), sleepers would never
  see `inflight.set()` and would block for the full 15 s wait every poll
  cycle.
* `event_emitter._client` was leaked on interpreter shutdown — the
  global singleton was never explicitly closed even though
  `atexit.register(reset_redis_clients)` covered other Redis users.
* `_blob_service` acquired `_BLOB_SERVICE_POOL_LOCK` on every call. With
  4-thread split-merge fan-outs * N concurrent BLAST submits the lock
  was hot enough to show up in flame graphs.

## User-facing change
None. Lower steady-state CPU on `/api/blast/jobs` polling; correct
recovery from a leader crash in the metadata cache; clean redis close
on uvicorn shutdown.

## API / IaC diff
* `api/services/blast_db_metadata.py`
  * `_DISPLAY_METADATA_INFLIGHT` value is now
    `(threading.Event, registered_at)`. Leader election skips entries
    older than `_DISPLAY_METADATA_INFLIGHT_TTL_SECONDS = 60.0` and
    wakes any sleepers before re-electing.
* `api/services/event_emitter.py`
  * `reset_for_tests()` now also closes the cached client.
  * `atexit.register(_atexit_cleanup)` so the global singleton is
    closed on interpreter shutdown.
* `api/services/storage_data.py`
  * `_BLOB_SERVICE_THREAD_LOCAL` adds a per-thread fast path. The hot
    read path (cache hit) no longer touches the global pool lock; cache
    misses still consult the global pool.
  * `reset_blob_service_pool` clears the calling thread's local cache
    (sibling threads re-resolve on their next call).

## Validation
* `uv run pytest -q api/tests/test_storage_data.py
  api/tests/test_blast_db_metadata.py
  api/tests/test_event_emitter.py` — 57 passed.
* `uv run ruff check` on the three changed files — clean.

## Items also reviewed (no code change)
* #19 (P2 `reconcile_stale_jobs` / `backfill_completed_runtime_metrics`
  select-projection) — both call sites use `row.payload`, so a
  payload-less SELECT would break correctness. Leaving as-is; per-call
  cost is bounded by the existing `limit=` and beat cadence.
* #20 (P2 `read_blob_text` cumulative budget) — every existing call site
  already passes a per-call `max_bytes`, and routes that call it serve
  one blob per request. No real cumulative path exists today.
