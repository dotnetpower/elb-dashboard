---
title: AKS Capacity Gate — Stage 5 telemetry counters
description: In-process admit/deny/release/reserve-lost counters exposed via the existing /api/blast/capacity route.
tags: [blast, architecture]
---

# AKS Capacity Gate — Stage 5 telemetry counters

Closes the final stage of the 5-stage plan for [issue #23](https://github.com/dotnetpower/elb-dashboard/issues/23).

Prior stages:
- [Stage 1 — capacity_gate module](./2026-05-30-stage1-capacity-gate-module.md)
- [Stage 2 — per-job workdir verification](./2026-05-30-stage2-per-job-workdir.md)
- [Stage 3a — capacity_signals helper](./2026-05-31-stage3a-capacity-signals.md)
- [Stage 3b — wire gate into submit_task](./2026-05-31-stage3b-submit-task-wiring.md)
- [Stage 3c — Bicep defaults + submit gate tests](./2026-05-31-stage3c-bicep-defaults.md)
- [Stage 4 — /api/blast/capacity route + SPA cell](./2026-05-31-stage4-capacity-route-and-cell.md)

## Motivation

Operators want to see _how often_ the gate admits / denies / releases without
SSH-ing into a worker pod and grep-ing `blast_gate_*` log lines. The metric
budget for this project is intentionally tiny — no Prometheus, no Application
Insights custom metric — so Stage 5 adds the cheapest thing that solves the
problem: per-cluster integer counters kept in memory on each worker and
surfaced through the already-deployed `/api/blast/capacity` route. The SPA
`CapacityGateCell` then renders a single line ("admit 12 · deny 3 · lost 1")
under the existing watermark bars.

The design relies on the Container App's single-revision invariant
(`minReplicas: 1, maxReplicas: 1`) — there is exactly one worker process, so
the in-process dict is the source of truth, no cross-process aggregation is
needed. Counters reset to zero on revision swap, which **matches** the
operator-visible window (a deploy is a natural "since when?" boundary). The
charter explicitly forbids introducing a managed Redis or a metrics
side-channel; this design honours that constraint.

## User-facing change

* `GET /api/blast/capacity` response gains a `counters` block:

  ```json
  "counters": {
    "admit_total": 12,
    "deny_total": 3,
    "release_total": 11,
    "reserve_lost_total": 1,
    "deny_by_reason": { "cpu_watermark": 2, "slot_cap_reached": 1 },
    "last_event_at": "2026-05-31T15:51:00+00:00"
  }
  ```

* SPA Capacity Gate cell shows a small counter strip below the watermark
  bars: `admit 12 · deny 3` (and `lost 1` when reserve-lost has ever fired).
  The strip is hidden when the backend returns no `counters` field, which
  preserves backward-compat with any cached client.

## API / IaC diff summary

* `api/services/blast/capacity_gate.py`:
  * Added `bump_admit / bump_deny / bump_release / bump_reserve_lost` and
    `gate_counters_snapshot` (module-level dict guarded by `threading.Lock`).
  * Added `_reset_counters_for_tests` (test-only helper, **not** exported in
    `__all__`).
  * `__all__` updated to include the five new public helpers.
* `api/tasks/blast/submit_task.py`:
  * Added one-line `capacity_gate.bump_*` call after every existing
    `blast_gate_*` LOGGER.info site (admit / deny / release / reserve_lost).
  * No behaviour change on the gate-disabled (default) path — counters are
    only bumped when the gate branch is entered.
* `api/routes/blast/capacity.py`:
  * Response payload gains `"counters": capacity_gate.gate_counters_snapshot(cluster_name)`.
* `web/src/api/blast.ts`:
  * Added `CapacityGateCounters` interface and `counters?: CapacityGateCounters`
    on `CapacityGateSnapshot`.
* `web/src/components/cards/ClusterBento/CapacityGateCell.tsx`:
  * Render a small counter strip below the watermark bars when `counters`
    is present.

No Bicep changes — counters live in memory; no infra surface to provision.

## Validation evidence

* `uv run pytest -q api/tests/test_blast_capacity_gate.py api/tests/test_blast_capacity_signals.py api/tests/test_blast_submit_capacity_gate.py api/tests/test_blast_capacity_route.py api/tests/test_blast_capacity_gate_counters.py`
  → **67 passed** (was 56 at end of Stage 4; +8 telemetry tests +
  4 submit-task counter integration tests).
* New `api/tests/test_blast_capacity_gate_counters.py`:
  8 unit tests covering zero defaults, per-cluster isolation, deny-by-reason
  grouping, defensive-copy semantics, empty-cluster fallback, and
  thread-safety under 200 concurrent `bump_admit` calls.
* New cases in `api/tests/test_blast_submit_capacity_gate.py`:
  4 integration tests covering admit+release, retryable deny, hard reject,
  and reserve-lost race — each asserts the matching counter increments by
  exactly 1.
* Updated `api/tests/test_blast_capacity_route.py`:
  added `_reset_counters` autouse fixture, asserted zero-defaults on the
  default-disabled test, and added `test_capacity_snapshot_surfaces_telemetry_counters`
  that pre-bumps every counter and asserts the route echoes them.
* `cd web && npm test -- --run src/components/cards/ClusterBento/CapacityGateCell.test.ts`
  → **7 passed**.

## Charter compliance

* §12a Rule 4 (default-OFF gates): counter bumps are inside the
  `BLAST_GATE_ENABLED=true` branch — when the gate is off the existing
  submit-lock path is byte-identical to pre-Stage 5.
* §12a Rule 5 (no `Depends(require_caller)` on SSE): counters are surfaced
  via the existing `require_caller`-protected `/api/blast/capacity` route.
  No SSE event stream is touched.
* §13 in-process state: counters reset on revision swap; this is the
  documented contract, not a bug. No Redis, no Cosmos, no metrics SDK
  added.
