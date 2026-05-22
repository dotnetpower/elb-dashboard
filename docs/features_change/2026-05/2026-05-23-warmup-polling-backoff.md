# wait_for_warmup_jobs — dedup state writes + adaptive poll backoff

## Motivation
`wait_for_warmup_jobs` polled `k8s_warmup_status` on a fixed 15 s
cadence and wrote the same `record_task_progress` + `update_state`
combo on every tick — including when nothing changed. A long warmup
(20-30 min) produced 80-120 identical Table writes; under multiple
concurrent DB warmups the workload Storage Table hit throttling.
The polling itself also fanned out 6 K8s GETs per call, multiplied
across every active warmup.

## User-facing change
Faster + cheaper warmup waits. The chip strip update latency stays
unchanged for actual transitions (still pulses at `poll_seconds`),
but quiet periods stop generating Table writes and stretch the K8s
poll cadence to 2x → 4x (capped at 60 s).

## API / IaC diff
* `api/tasks/storage/helpers.py::wait_for_warmup_jobs`
  * Track a `(nodes_ready, nodes_failed, nodes_active, total_jobs)`
    signature; skip `record_task_progress` + `update_state` when the
    signature is unchanged from the previous tick.
  * `quiet_ticks` counter: after 3 unchanged ticks sleep `2 *
    poll_seconds`, after 6 ticks sleep `4 * poll_seconds`, hard
    ceiling 60 s.

## Validation
* `uv run pytest -q api/tests -k warmup` — 84 passed.
* `uv run ruff check api/tasks/storage/helpers.py` — clean.
