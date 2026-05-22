# Bound worker memory: exec_server output cap + Celery lifecycle limits

## Motivation
Three orthogonal sources of unbounded growth across the worker / terminal
sidecars:

1. **`exec_server._run_buffered`** used `proc.communicate()`, which loads
   the child's full stdout + stderr into the terminal sidecar's RAM (then
   another copy when we decode to UTF-8). A verbose `elastic-blast submit`
   or `az --debug` run could emit tens of MB and OOM the sidecar.
2. **`celery_app.conf`** had no `worker_max_tasks_per_child`, so a worker
   process accumulated allocator fragmentation plus one-shot dependency
   leaks (XML parsers, gzip buffers, K8s clients, Azure SDK pipelines)
   indefinitely. Steady BLAST traffic pushes the worker RSS into multi-GB
   territory before any restart.
3. **`celery_app.conf`** had no `task_time_limit` / `task_soft_time_limit`,
   so a hung `terminal_exec` stream, stuck Kubernetes wait, or runaway
   Storage call held a worker slot forever.

## User-facing change
None directly. Steady-state worker / terminal RSS stays bounded. The HTTP
response from `terminal_exec.run()` now also carries `stdout_truncated` /
`stderr_truncated` booleans so callers can degrade cleanly if output was
capped.

## API / IaC diff
* `terminal/exec_server.py`
  * `_run_output_max_bytes()` resolves the cap from
    `EXEC_RUN_MAX_OUTPUT_BYTES` (default 8 MiB) at request time so ops can
    rotate the limit without redeploy and tests can override per-call.
  * `_drain_capped(pipe, cap)` reads from a pipe until EOF, keeping at
    most `cap` bytes; over-cap bytes are discarded but the pipe keeps
    being drained so the child does not block on a full SIGPIPE-style
    backpressure.
  * `_run_buffered` replaces `proc.communicate()` with two reader threads
    feeding `_drain_capped`. Response gains `stdout_truncated` /
    `stderr_truncated` flags.
* `api/celery_app.py`
  * `worker_max_tasks_per_child=200` (override `CELERY_WORKER_MAX_TASKS_PER_CHILD`).
  * `task_soft_time_limit=3300` / `task_time_limit=3600` (1 h hard ceiling).
  * `result_expires=3600` so Redis db 1 does not retain stale dicts
    (preemptively addresses #27 in the same config block).
* `api/tests/test_terminal_exec.py` adds
  `test_run_truncates_stdout_above_cap` to lock in the 64 KiB cap path
  end-to-end.

## Validation
* `uv run pytest -q api/tests/test_terminal_exec.py` — 15 passed (new
  cap test included; concurrency/timeout coverage unchanged).
* `uv run ruff check api/celery_app.py terminal/exec_server.py
  api/tests/test_terminal_exec.py` — clean.
