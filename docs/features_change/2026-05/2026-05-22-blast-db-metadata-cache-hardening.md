# 2026-05-22 — BLAST DB metadata cache hardening (4-step rollout)

## Motivation

`/api/blast/jobs/{id}?include_database_metadata=true` was costing 700-1500 ms
per call because the backend re-downloaded 2-4 Storage blobs (`.njs` 3-path
fallback + `{db}-metadata.json`) on every page render, with no caching and a
fresh `BlobServiceClient` per call. The Job detail page renders that block on
every navigation, so users felt the dashboard "always reloads the DB info".

The existing in-page React Query `staleTime: Infinity` only covered intra-page
caching; backend-side everything was cold every request.

## User-facing change

- Job detail page DB metadata block loads in < 30 ms after the first hit.
- After an admin runs `prepare-db` or a Celery `warmup_database` finishes, the
  new title / sequence count / shard layout shows up on the very next page
  poll across **every** sidecar (api / worker / beat), not just whichever one
  ran the write.
- No change in shape of the response payload.

## API / IaC diff

No HTTP contract changes. New optional env vars (defaults are production-safe):

| Env | Default | Effect |
|---|---|---|
| `BLAST_DB_METADATA_CACHE_TTL` | `86400.0` (24 h) | Display metadata TTL. With explicit invalidation in place the long TTL is safe; lower for short experiments. |
| `BLAST_DB_METADATA_INVALIDATE_DISABLED` | unset (= enabled) | Disable the cross-sidecar pub/sub channel + subscriber. Tests set this to `true`. |
| `BLAST_DB_METADATA_INVALIDATE_CHANNEL` | `elb:cache:blast-db-metadata` | Override channel name (multi-tenant local dev). |
| `NCBI_LATEST_DIR_CACHE_TTL` | `3600.0` (1 h) | NCBI `latest-dir` HTTP cache. |
| `NCBI_LIST_KEYS_CACHE_TTL` | `3600.0` (1 h) | NCBI bucket listing cache per `(latest_dir, db_name)`. |

## Implementation overview (4 staged commits worth)

### Step 1 — prepare-db invalidate + 24 h TTL
- [api/services/blast_db_metadata.py](../../../api/services/blast_db_metadata.py): added
  `invalidate_blast_db_metadata_cache(account, db)` (precise / per-account /
  global modes), raised TTL default to 24 h, kept the existing
  `_reset_blast_db_metadata_cache` test hook as a thin alias.
- [api/routes/storage/prepare_db.py](../../../api/routes/storage/prepare_db.py):
  `_write_db_metadata(...)` now takes `account_name` and invalidates the cache
  after every write (start / final / failure) so an admin action is visible
  on the next poll.

### Step 2 — BlobServiceClient per-account pool
- [api/services/storage_data.py](../../../api/services/storage_data.py):
  `_blob_service()` now returns a process-shared client per
  `(id(credential), account_name)` with LRU eviction (max 32). Eviction
  `close()` runs outside the pool lock so a slow shutdown can't block other
  callers. `reset_blob_service_pool()` test hook + automatic invocation from
  `reset_credential()` so a stale token cache never outlives its credential.

### Step 3 — Redis pub/sub cross-sidecar invalidate
- [api/services/blast_db_metadata.py](../../../api/services/blast_db_metadata.py):
  added `publish_blast_db_metadata_invalidate(...)` and the convenience
  `notify_blast_db_metadata_changed(...)` (local invalidate + publish).
  `start_invalidate_subscriber()` spawns a daemon thread that uses
  `pubsub.get_message(timeout=1.0)` (not `listen()`) so `stop_event` is
  honoured within ~1 s. Reconnects with exponential backoff capped at 30 s.
- [api/main.py](../../../api/main.py): wired start/stop into the FastAPI
  lifespan.
- [api/tasks/storage/__init__.py](../../../api/tasks/storage/__init__.py):
  `warmup_database` now publishes after every `{db}-metadata.json` write so
  the api sidecar drops its cache as soon as worker sharding finishes.

### Step 4 — NCBI catalogue cache
- [api/routes/storage/common.py](../../../api/routes/storage/common.py):
  `_resolve_latest_dir()` and `_list_keys()` are cached for 1 h each.
  Snapshot contents are immutable, so caching is safe without explicit
  invalidation.

### Step 5 — critical review hardening (folded into the same change)
- **Cache stampede**: `resolve_database_display_metadata` now coordinates
  concurrent cache misses via a per-key `threading.Event` (single-flight). N
  concurrent callers share one Storage round-trip instead of paying it N
  times on every TTL boundary.
- **Mutable cache value**: `copy.deepcopy` on read so caller mutations cannot
  poison the cache for other callers.
- **Pool eviction under lock**: close runs outside the lock.
- **Subscriber stop responsiveness**: `get_message(timeout=1.0)` replaces
  `listen()` and `pubsub.close()` runs in a `finally` block so the
  connection is released even on backoff retry.

## Validation evidence

```
$ uv run ruff check api
All checks passed!
$ uv run pytest -q api/tests
915 passed in 24.69s
```

New / extended tests:

- [api/tests/test_blast_db_metadata.py](../../../api/tests/test_blast_db_metadata.py):
  `test_resolve_database_display_metadata_caches_storage_lookups`,
  `test_invalidate_blast_db_metadata_cache_drops_one_db`,
  `test_invalidate_blast_db_metadata_cache_drops_whole_account`,
  `test_invalidate_blast_db_metadata_cache_global_clear`,
  `test_publish_blast_db_metadata_invalidate_no_op_when_disabled`,
  `test_publish_blast_db_metadata_invalidate_calls_redis_publish`,
  `test_notify_blast_db_metadata_changed_invalidates_locally_and_publishes`,
  `test_resolve_display_metadata_returns_independent_copy_per_call`,
  `test_resolve_display_metadata_single_flight_on_cache_miss`,
  `test_stop_invalidate_subscriber_signals_exit`.
- [api/tests/test_storage_data.py](../../../api/tests/test_storage_data.py):
  `test_blob_service_pool_returns_same_instance_for_same_account`,
  `test_blob_service_pool_distinct_per_account_and_credential`,
  `test_blob_service_pool_evicts_lru_when_over_capacity`,
  `test_reset_blob_service_pool_closes_clients`.
- [api/tests/test_storage_common_cache.py](../../../api/tests/test_storage_common_cache.py)
  (new): NCBI catalogue cache behaviour.

## Risk + known limits

- Cross-sidecar invalidation is best-effort; if Redis is unreachable a stale
  cache entry can survive up to `BLAST_DB_METADATA_CACHE_TTL` (24 h). Lower
  the TTL temporarily if this becomes an operational concern.
- The single-flight wait timeout is 15 s; if the leader stays slower than
  that, late waiters fall back to leader-electing themselves. This is the
  same safety-valve pattern used by `_external_list_jobs_cached`.
- `databases` list endpoint is NOT yet cached — it has many write paths (new
  blobs, deletions, shard rebuilds) and clean invalidation requires more
  groundwork. Deferred; existing per-call performance is acceptable.
