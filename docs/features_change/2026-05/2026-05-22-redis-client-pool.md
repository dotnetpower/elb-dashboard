# Redis client pool — stop per-call `from_url` leak

## Motivation
Multiple modules were calling `redis.Redis.from_url(...)` on every invocation:

* `api/tasks/blast/submit_lock.py::acquire_submit_lock` — once per BLAST submit
* `api/services/blast_db_metadata.py::publish_blast_db_metadata_invalidate`
* `api/services/auto_warmup_reconcile.py::autowarmup_inflight_redis`
* `api/services/openapi_runtime.py` — 5 call sites
* `api/services/sidecar_metrics.py::collect_snapshot` — every monitor poll
* `api/routes/health.py` — every `/api/health` extended probe
* `api/services/blast_db_metadata.py` cache-invalidate subscriber thread on every reconnect

Each call built a fresh `ConnectionPool` + socket; release/cleanup never closed
the client, so steady BLAST traffic leaked Redis connections (and FDs) until
the worker / api sidecar hit `RLIMIT_NOFILE`.

## User-facing change
None directly. Backend memory + FD usage stays flat under sustained BLAST
submit + dashboard polling. Worker and api sidecars survive longer between
restarts.

## API / IaC diff
* New module `api/services/redis_clients.py` exposing
  `get_redis_client(url, **kwargs)`, `get_ops_redis_client(**kwargs)`,
  `get_broker_redis_client(**kwargs)`, and `reset_redis_clients()` (test
  hook + `atexit` cleanup).
* Cache key is `(url, frozenset(kwargs.items()))` so callers passing
  identical kwargs share one pool; distinct kwargs (e.g. different
  `socket_timeout`) get distinct pools.
* All call sites listed above now route through the shared helper instead
  of allocating per call. `acquire_submit_lock` returns a shared client —
  callers MUST NOT call `.close()` on it (documented in the lock module
  header).
* `api/conftest.py` adds `reset_redis_clients()` to the autouse cleanup
  fixture so test isolation is preserved.

## Validation
* `uv run pytest -q api/tests/test_redis_clients.py` (7 passed, new file)
* `uv run pytest -q api/tests/test_blast_db_metadata.py
  api/tests/test_blast_tasks.py api/tests/test_sidecar_metrics.py` —
  140 + 33 passed (regression suite)
* `uv run ruff check api/services/redis_clients.py
  api/tasks/blast/submit_lock.py api/services/blast_db_metadata.py
  api/services/auto_warmup_reconcile.py api/services/openapi_runtime.py
  api/services/sidecar_metrics.py api/routes/health.py
  api/tests/test_redis_clients.py` — clean
