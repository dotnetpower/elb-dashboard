# Fix Service Bus CI hang (BlobServiceClient pool finalizer self-deadlock) + examples doc

**Date:** 2026-06-17
**Area:** Storage client pool (`api/services/storage/client_pool.py`), docs

## Motivation

The GitHub Actions **Tests** workflow was hanging until the pytest session
timeout (red build on `main` since at least 2026-06-16, unrelated to the commit
that surfaced it). The CI traceback showed a single thread holding
`_BLOB_SERVICE_POOL_LOCK` inside `BlobServiceClient(...)` construction while, on
the **same thread**, a `weakref` credential finalizer fired during garbage
collection and tried to acquire the same non-reentrant lock — a classic
self-deadlock.

## User-facing change

None at runtime. CI stability fix plus a new documentation page.

## Fix summary

`api/services/storage/client_pool.py`:

- The credential weakref finalizer body is now the module-level
  `_evict_credential_or_defer(target_id)`, which acquires the pool lock
  **non-blocking**. If the lock is busy (the GC-on-the-same-thread case) it
  records the credential id in `_PENDING_CRED_EVICTIONS` and returns immediately
  instead of blocking — eliminating the self-deadlock.
- Deferred evictions are drained under the lock by the next pool operation
  (`_drain_pending_evictions_locked`), so no eviction is lost.
- `BlobServiceClient(...)` is now constructed **outside** the pool lock, so the
  allocation/GC window no longer overlaps a held lock (defense in depth). A
  concurrent builder for the same key discards its redundant client.
- `reset_blob_service_pool` also clears the pending set.

## Tests

- New `api/tests/test_storage_client_pool.py` (3 tests, `@pytest.mark.timeout(15)`):
  the finalizer defers instead of deadlocking when the lock is held, the pending
  eviction is drained by the next pool op, and a lock-free finalizer evicts inline.
- `api/tests/test_storage_data.py` (38), `test_storage_common_cache.py`,
  `test_storage_network.py` — all green (no behavior regression).

## Docs

- New page `docs/architecture/service-bus-examples.md` (wired into `nav`): a run
  guide for the `example/servicebus` producer/monitor/consumer scripts, the JSON
  contracts (request + completion event with `download_url`), RBAC requirements,
  and the verified end-to-end download walkthrough.

## Validation

- `uv run pytest -q -n 0 api/tests/test_storage_client_pool.py api/tests/test_storage_data.py api/tests/test_storage_common_cache.py api/tests/test_storage_network.py` — all pass.
- `uv run ruff check api/services/storage/client_pool.py api/tests/test_storage_client_pool.py` — clean.
- `uv run python scripts/docs/check_frontmatter.py` + `mkdocs build --strict` — green (56 navigated pages).
