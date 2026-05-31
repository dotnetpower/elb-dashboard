---
title: BLAST capacity gate — Stage 3 (submit-task wiring + Bicep env defaults)
description: Wire the BLAST capacity gate into the submit Celery task behind a default-OFF env flag, and add the matching Container Apps environment defaults across api/worker/beat sidecars.
tags: [blast, architecture, infra]
---

# BLAST capacity gate — Stage 3 (submit-task wiring + Bicep env defaults)

> Issue: [#23](https://github.com/dotnetpower/elb-dashboard/issues/23) — *AKS Capacity Gate for BLAST submit*.
> Builds on: [Stage 1 change note](2026-05-31-capacity-gate-stage1-tests.md), [Stage 2 change note](2026-05-31-stage2-workdir-isolation.md), [Stage 3a change note](2026-05-31-stage3a-capacity-signals.md).

## Motivation

Stage 1 shipped the pure capacity gate primitives. Stage 3a shipped the live
signal resolver (`api.services.blast.capacity_signals`). Stage 3 closes the
loop by wiring both into the BLAST submit Celery task — but does so behind a
default-OFF feature flag so the rollout is reversible per Charter §12a Rule 4.

Without the gate, BLAST submits serialize through a single per-cluster Redis
lock (`acquire_submit_lock`) which is equivalent to `max_slots=1`. Flipping
`BLAST_GATE_ENABLED=true` swaps that for the cluster-aware admission control
that watches CPU/memory request watermarks, counts pending pods, and reserves
a slot atomically before invoking `elastic-blast submit`.

## User-facing change

Nothing visible by default. After the rollout owner sets
`BLAST_GATE_ENABLED=true` on the api / worker / beat sidecars:

- New job state phases the dashboard renders:
  - `waiting_for_capacity` (replaces `waiting_for_submit_slot` once the gate
    is the active admission control) — temporary, retryable; the dashboard
    surfaces `retry_after_seconds: 30`.
  - `rejected_capacity` — hard reject, `retry_after_seconds: 600`. Used when
    a single submit exceeds the cluster's headroom by definition (e.g.
    estimated demand exceeds total cluster capacity).
- Worker logs gain three structured events: `blast_gate_admit`,
  `blast_gate_deny`, `blast_gate_release`, plus the race-loss line
  `blast_gate_reserve_lost`.

## API / IaC diff summary

### `api/tasks/blast/submit_task.py`
- Added `_capacity_gate_enabled()` helper — parses `BLAST_GATE_ENABLED`
  (truthy values: `1 true yes on`, case-insensitive). Default OFF.
- Wrapped the critical submit-time section in `if gate_enabled / else`:
  - **gate_enabled=True**: calls
    `capacity_signals.resolve_capacity_signals` →
    `capacity_gate.list_active_reservations` → `predict_demand` →
    `evaluate_capacity_gate`. On retryable deny: `_update_state` with
    phase=`waiting_for_capacity`, requeues via `submit.apply_async` with
    `countdown=30`. On non-retryable deny: phase=`rejected_capacity`,
    status=`failed`. On admit: `reserve_slot`; if the atomic reserve loses
    the race (`reserve_slot` returns `None`), treats it as a retryable deny
    with `error_code="capacity_reserve_lost"`.
  - **gate_enabled=False (default)**: byte-equivalent to the previous
    `acquire_submit_lock` path — required by Charter §12a Rule 4.
- `finally` block: when `submit_lock` is present, releases the lock; when
  `capacity_reservation` is present, calls `capacity_gate.release_slot` and
  emits a `blast_gate_release` log line.

### `infra/modules/containerAppControl.bicep`
- Added `BLAST_GATE_ENABLED=false` to the `env` lists of three sidecars
  (`api`, `worker`, `beat`). Default is intentionally `'false'` so a fresh
  `azd up` ships with the gate off; flipping the rollout to ON is an
  operational env edit (no Bicep change needed beyond bumping the default in
  a future PR).
- The optional knobs read by `api.services.blast.capacity_gate` and
  `capacity_signals` are documented inline next to the api sidecar entry:
  `BLAST_GATE_MAX_SLOTS_PER_CLUSTER` (default 1),
  `BLAST_GATE_CPU_WATERMARK_PCT` (default 75),
  `BLAST_GATE_MEM_WATERMARK_PCT` (default 75),
  `BLAST_GATE_SIGNAL_CACHE_S` (default 30).

### Tests
- New: `api/tests/test_blast_submit_capacity_gate.py` (7 tests).
  - `_capacity_gate_enabled` parses truthy / falsey env values.
  - Gate-disabled (default): asserts `acquire_submit_lock` is called once
    and `capacity_gate.{evaluate,reserve,release}_slot` are never touched.
  - Gate-enabled admit: `reserve_slot` is called with the predicted demand
    and `release_slot` runs in the `finally` block.
  - Gate-enabled retryable deny: phase=`waiting_for_capacity`, status=
    `running`, `submit.apply_async(countdown=30, queue="blast")` requeue.
  - Gate-enabled hard reject: phase=`rejected_capacity`, status=`failed`,
    no requeue.
  - Reserve-lost race: admit decision but `reserve_slot` returns `None` —
    requeues with `error_code="capacity_reserve_lost"`.

## Validation evidence

```
$ cd /home/moonchoi/dev/elb-dashboard && uv run pytest -q \
    api/tests/test_blast_submit_capacity_gate.py \
    api/tests/test_blast_capacity_gate.py \
    api/tests/test_blast_capacity_signals.py \
    api/tests/test_blast_tasks.py \
    api/tests/test_terminal_exec_workdir.py
... 172 passed in ~4s ...
$ uv run ruff check api/tasks/blast/submit_task.py \
    api/services/blast/capacity_signals.py \
    api/tests/test_blast_capacity_signals.py \
    api/tests/test_terminal_exec_workdir.py \
    api/tests/test_blast_submit_capacity_gate.py
All checks passed!
```

## Phase 1 / Phase 2 / Phase 3 rollout (operational, not code)

| Phase | Action | Owner |
|-------|--------|-------|
| Phase 1 | Ship Stage 1 + Stage 3 with default OFF. Soak. | done in this change |
| Phase 2 | Set `BLAST_GATE_ENABLED=true` on api+worker+beat env. Verify dashboard shows `waiting_for_capacity` instead of `waiting_for_submit_slot` under contention. | operator |
| Phase 3 | After ≥1 release cycle in Phase 2, optionally raise `BLAST_GATE_MAX_SLOTS_PER_CLUSTER` from 1. **Blocker**: see Stage 2 note — upstream elastic-blast's K8s `metadata.name` does not embed `${BLAST_ELB_JOB_ID}` per submit, so concurrent submits in the same cluster/namespace collide today. Stage 2 deferred to upstream. | requires upstream elastic-blast change |
