# stream_blob_bytes — wrap every active transfer in a bounded semaphore

## Motivation
The module header comment promised "semaphore-capped to 4 concurrent
transfers" but the actual code had no semaphore. Ten simultaneous browser
tab opens could each pin one BlobServiceClient HTTP connection and the
api/worker thread serving them, starving every other in-flight Storage
call.

## User-facing change
None on the happy path. Under high concurrency, downloads queue at the
sidecar instead of competing for the same connection pool; if the queue
wait exceeds `STORAGE_STREAM_ACQUIRE_TIMEOUT_SECONDS` (default 30 s) the
helper raises `RuntimeError("storage download semaphore exhausted…")`
so the caller can return a clear 503 instead of hanging.

## API / IaC diff
* `api/services/storage_data.py`
  * Module-level `_STREAM_DOWNLOAD_SEMAPHORE =
    threading.BoundedSemaphore(_STREAM_DOWNLOAD_MAX_CONCURRENCY)`
    (default 4, env `STORAGE_STREAM_MAX_CONCURRENCY`).
  * `stream_blob_bytes(...)` acquires the permit before opening the
    downloader and releases it in a `finally` after the generator
    finishes — including the consumer-abandonment path because Python's
    generator GC triggers the `finally` clause.
  * Acquire uses `timeout=_STREAM_DOWNLOAD_ACQUIRE_TIMEOUT_SECONDS`
    (default 30 s) so a stuck consumer cannot block new requests
    indefinitely.

## Validation
* `uv run pytest -q api/tests/test_storage_data.py` — 31 passed.
* `uv run ruff check api/services/storage_data.py` — clean.
