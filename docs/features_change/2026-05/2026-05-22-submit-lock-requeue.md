# BLAST submit — stop consuming retry budget on lock contention

## Motivation
When two BLAST submits raced for the same `(cluster, namespace)` lock the
loser called `_retry_or_fail(..., retry_after_seconds=30)`. That path
consumes Celery's `max_retries=12` budget — six minutes of lock contention
permanently failed the submit even though contention is the expected
shape, not an error. Worse, every retry was a fresh Celery `task.retry()`
call that the broker stored as an in-flight task, pinning broker memory.

## User-facing change
Long-running submit queues are now genuinely fair. A submit that loses
the lock immediately ends the current Celery task (no broker-side
retry-state record), parks the job row as `waiting_for_submit_slot`, and
re-enqueues itself with a 30 s countdown. The dashboard keeps showing the
row with the same state code as before; the retry counter on the original
task stays at 0 so real transient failures (terminal sidecar down, etc.)
keep their full 12-retry budget.

## API / IaC diff
* `api/tasks/blast/submit_task.py` — when `acquire_submit_lock(...)`
  returns `None`, write the `submit_lock_busy` state row and call
  `submit.apply_async(..., countdown=30, queue="blast")` for the same
  job_id, returning early with `{"phase": "waiting_for_submit_slot",
  "requeued": True}`. If the re-enqueue itself fails (broker gone), fall
  back to `_retry_or_fail` so the broker error surfaces.

## Validation
* `uv run pytest -q api/tests/test_blast_tasks.py -k submit` — 26 passed
  (no test asserted on the legacy "consume max_retries" path; the
  contention paths covered remain green).
* `uv run ruff check api/tasks/blast/submit_task.py` — clean.
