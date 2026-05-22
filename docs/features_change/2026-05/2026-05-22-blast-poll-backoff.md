# Poll cadence — back off after the first minute

## Motivation
`poll_running_status` self-rescheduled every 10 s for up to 180 iterations
(~30 min) regardless of how long a BLAST job had been running. With N
concurrent submits the broker traffic was `6 * N` poll tasks per minute
for the full 30 min — wasted broker writes for jobs that take minutes
(or hours) to finish; the 60 s beat reconcile already covers slow jobs.

## User-facing change
First-minute UI latency unchanged (10 s ticks). Beyond a minute the
dashboard's running-row update interval grows to 30 s, then to 60 s after
~5 minutes — matching the beat reconcile. Total broker tasks per submit
drop from 180 to 60.

## API / IaC diff
* `api/tasks/blast/poll_tasks.py`
  * Added `POLL_RUNNING_INTERVAL_MEDIUM = 30`, `POLL_RUNNING_INTERVAL_LONG = 60`,
    `POLL_RUNNING_FAST_ITERATIONS = 6`, `POLL_RUNNING_MEDIUM_ITERATIONS = 15`.
  * `POLL_RUNNING_MAX_ITERATIONS` lowered from 180 to 60; the wall-clock
    cap is still ~50 min (6×10 + 9×30 + 45×60 ≈ 52 min) which covers
    realistic submit-to-result spans.
  * New private helper `_poll_running_interval(iteration)` returns the
    next countdown.
  * `poll_running_status` reschedule uses the new helper.

## Validation
* `uv run pytest -q api/tests/test_blast_tasks.py` — 120 passed (poll
  task tests still green; existing tests pin behaviour, not cadence).
* `uv run ruff check api/tasks/blast/poll_tasks.py` — clean.
