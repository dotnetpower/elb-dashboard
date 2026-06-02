---
title: BLAST submit re-enqueues for warmup instead of failing
description: A retryable node-local warmup state now re-enqueues the BLAST submit (waiting_for_warmup, status running like the capacity gate) instead of failing the job; the baseline run profile skips the warmup gate and runs immediately.
tags:
  - blast
---

# BLAST submit re-enqueues for node-local warmup instead of failing

## Motivation

Submitting a BLAST search while the node-local DB warmup was still settling
produced an immediate **Job Failed at Warmup Check** with
`node_warmup_not_ready` (e.g. `node warmup for core_nt has no DB generation
marker`). The warmup states that trigger this — `Loading` / `Pending` /
`Starting`, a freshly-`Ready` job that has not yet written its DB generation
marker, or a transient warmup-status read failure — are all **transient**: the
search would have succeeded a few seconds later. Forcing the researcher to
re-submit by hand was unnecessary friction.

A first cut used `task.retry` (via the shared `_retry_or_fail` helper). That
worked but was inferior to the pattern the **same function already uses 40 lines
below** for its capacity gate and submit lock: re-enqueue with
`submit.apply_async(..., countdown=30, queue="blast")`. `task.retry` consumes
the task's `max_retries=12` budget and caps the wait at the backoff ceiling
(~6 min); a re-enqueue is unbounded and consumes no retry budget, so a search
against a slowly-warming sharded DB keeps its place in line until the shards
report warm. This change switches the warmup wait to the re-enqueue pattern for
consistency.

## User-facing change

`WarmupNotReadyError` carries a `retryable` flag. The submit handler now
respects it the same way the capacity gate does:

* **Retryable warmup state** → the submit is **re-enqueued** (`apply_async`,
  30 s countdown, `blast` queue) instead of failing. The waiting row keeps
  `status="running"` on the `waiting_for_warmup` phase — exactly like the
  capacity gate's `waiting_for_capacity` row — so the SPA shows a calm active
  state (the phase still maps to the **Warmup Check** step) and, critically, the
  reconciler keeps the job active (see below). The search starts automatically
  once the shards report warm. The wait has **no ~6-minute ceiling** and does
  **not** consume `max_retries`.
* **Non-retryable warmup error** (e.g. a missing database name) → still fails
  immediately with `node_warmup_not_ready`, unchanged.
* If the re-enqueue itself fails (broker unreachable) → it falls back to the
  bounded `_retry_or_fail` path with `error_code="blast_submit_requeue_failed"`
  so the broker error surfaces instead of being swallowed.

### Bounded by a generous warmup-wait deadline

The re-enqueue loop is otherwise unbounded, so a *permanently* stuck warmup (a
node that never leaves `Loading`, a DB generation marker that never lands) would
re-enqueue forever and never surface as **Failed**. The first
`waiting_for_warmup` re-enqueue now stamps a deadline
(`now + BLAST_WARMUP_MAX_WAIT_SECONDS`, default **45 minutes**) into the
re-enqueue message as `warmup_wait_deadline_ts`, and every subsequent re-enqueue
forwards it unchanged. Once the deadline passes, the job fails fast with
`status="failed"`, `phase="warmup_not_ready"`,
`error_code="node_warmup_wait_deadline_exceeded"` instead of looping. This keeps
the normal transient case auto-resuming while giving a genuinely stuck warmup a
real terminal state. (The deadline resets if a revision restart rebuilds the
queue from the jobstate table — acceptable for such a rare event.)

### Why the waiting row is `status="running"`, not `"queued"`

The original task returns SUCCESS the moment it re-enqueues, and the state row's
`task_id` still points at that original task. When `reconcile_stale_jobs` next
runs it sees the terminal SUCCESS and asks `_celery_success_row_status` what to
do with the result. That helper only keeps `status="running"` active; any other
status (including `"queued"`) falls through to `("completed", phase)`, which
would mark the still-waiting job **completed** and fire the artifact finalizer on
a job that has not run yet. Using `status="running"` (the value the capacity gate
already uses for the identical reason) keeps the re-enqueued job active until it
genuinely finishes. A regression test
(`test_requeued_warmup_row_stays_active_in_reconciler`) pins this contract.

### Baseline run profile runs immediately

The **baseline** run profile (New Search → Compute) sets `enable_warmup: false`.
`submit_requires_node_warmup` short-circuits to `False` whenever
`enable_warmup is False`, so `ensure_node_warmup_ready_for_submit` returns `None`
without ever polling the K8s warmup status — the warmup gate (and its
`WarmupNotReadyError`) never runs. A baseline search therefore **skips the
warmup wait entirely and runs immediately**; only warmup-enabled / sharded
profiles can enter the `waiting_for_warmup` queued state above.

## API / IaC diff summary

* [api/tasks/blast/state.py](../../../api/tasks/blast/state.py) —
  `_retry_or_fail` is **reverted to its original signature** (the short-lived
  `retry_status` / `fail_phase` knobs the `task.retry` cut had added are
  removed, since the re-enqueue path no longer needs them). It again writes a
  `status="running"` retry-scheduled row with an exponential countdown capped at
  300 s, matching its module header.
* [api/tasks/blast/submit_task.py](../../../api/tasks/blast/submit_task.py) —
  the `except WarmupNotReadyError` branch in `submit()` now, when
  `exc.retryable` is true, writes a `running` `waiting_for_warmup` row and
  re-enqueues via `submit.apply_async(kwargs={…original options…}, countdown=30,
  queue="blast")`, returning `{status:"running", phase:"waiting_for_warmup",
  requeued:True}` — byte-for-byte the capacity gate's pattern (including the
  `status="running"` value that keeps the reconciler from completing it early).
  The non-retryable path writes a `failed` `warmup_not_ready` row (now also
  carrying `output=` and `error_code="node_warmup_not_ready"` for parity with the
  `database_unavailable` path). A new optional `warmup_wait_deadline_ts` kwarg
  (internal re-enqueue only; external callers never pass it) plus
  `_warmup_max_wait_seconds()` (env `BLAST_WARMUP_MAX_WAIT_SECONDS`, default
  2700 s) bound the wait — once exceeded the job fails with
  `error_code="node_warmup_wait_deadline_exceeded"`.
* No IaC change.

## Validation evidence

* `uv run pytest -q api/tests/test_blast_submit_warmup_retry.py` — 9 passed:
  retryable warmup re-enqueues as `running` on `waiting_for_warmup`
  (`requeued=True`, `apply_async` countdown 30 / queue `blast`, original options
  forwarded) and never writes `failed`; the first re-enqueue stamps a deadline
  and later re-enqueues forward it; an exceeded deadline fails fast as
  `node_warmup_wait_deadline_exceeded` with no further re-enqueue; non-retryable
  warmup fails fast as `warmup_not_ready` and never re-enqueues; a broker failure
  during re-enqueue falls back to `_retry_or_fail` with
  `blast_submit_requeue_failed`; the requeued SUCCESS result is reconciled as
  still-active (`running`), not `completed`; and the baseline profile
  (`enable_warmup:false`) makes `submit_requires_node_warmup` False and
  `ensure_node_warmup_ready_for_submit` return `None` **without polling**
  `k8s_warmup_status`.
* `uv run pytest -q api/tests/test_blast_tasks.py
  api/tests/test_blast_submit_capacity_gate.py
  api/tests/test_blast_submit_accession.py` — 149 passed (no regressions).
* `uv run ruff check api/tasks/blast/state.py
  api/tasks/blast/submit_task.py api/tests/test_blast_submit_warmup_retry.py` —
  clean.
