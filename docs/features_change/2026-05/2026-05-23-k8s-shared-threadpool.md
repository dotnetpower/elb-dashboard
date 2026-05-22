# k8s_monitoring — shared ThreadPoolExecutor (drop per-call spawn)

## Motivation
`k8s_warmup_status` and `_warmup_pods_and_logs` each created a fresh
`ThreadPoolExecutor(...)` per call via `with` blocks. On every monitor
poll (4-8 s dashboard cadence × multiple users) the worker spawned and
tore down 6 + 12 threads — `pthread_create` cost plus Python's
`_thread.start_new_thread` overhead added up.

## User-facing change
None. Same fan-out behaviour, lower per-call overhead, no thread
exhaustion under heavy polling.

## API / IaC diff
* `api/services/k8s_monitoring.py`
  * Added `_k8s_fanout_pool()` returning a process-shared
    `ThreadPoolExecutor(max_workers=_K8S_FANOUT_POOL_MAX_WORKERS=16)`,
    with env override `K8S_FANOUT_POOL_MAX_WORKERS`.
  * `atexit.register(_shutdown_k8s_fanout_pool)` so the pool is torn
    down on interpreter shutdown.
  * `k8s_warmup_status` and `_warmup_pods_and_logs` now reuse the
    shared pool via `pool.submit(...)` / `pool.map(...)` instead of
    spawning a new executor per call.

## Validation
* `uv run pytest -q api/tests/test_k8s_warmup_status_parallel.py
  api/tests/test_k8s_release_stale_warmup_jobs.py` — 8 passed.
* `uv run ruff check api/services/k8s_monitoring.py` — clean.
