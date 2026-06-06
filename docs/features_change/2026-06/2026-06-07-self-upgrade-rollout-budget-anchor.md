---
title: Self-upgrade rollout budget — anchor to PATCH moment, not upgrade start
description: Measure the rolling_out stuck-guard from when the ARM PATCH landed
  instead of from the overall upgrade start, so the ~10-13 min clone+build
  phase no longer eats the rollout window and false-aborts a healthy new
  revision seconds into booting.
tags:
  - operate
  - deployment-reference
---

# Self-upgrade rollout budget — anchor to the PATCH moment

## Motivation

Even after the reconciler learned to detect success via the deployed image
tag (see [self-upgrade reconciler success](2026-06-07-self-upgrade-reconciler-success.md)),
a commit-channel self-upgrade that built all three images and brought up a
new revision (`…--v0-2-0-commit-9290827-…` live and `RunningAtMaxScale`) still
ended in `failed_rollout` with `rolling_out exceeded budget (985s)`.

## Root cause

The `rolling_out` stuck-guard in
[api/tasks/upgrade/reconciler.py](../../../api/tasks/upgrade/reconciler.py)
computed its elapsed time from `row.started_at` — the moment the **whole**
upgrade began (`queued`). But `started_at` already absorbs the entire
clone + 3× `az acr build` phase (~10-13 min). By the time the row entered
`rolling_out`, ~13 min of the 15-min (`ROLLING_OUT_TIMEOUT_SECONDS`) budget
was already gone, leaving only ~2 min for the new revision to pull its image
and pass its readiness probe. A perfectly healthy revision that took longer
than that to boot was failed at 985s — the success branch never got the
chance to fire because the budget guard runs first.

The blue/green path already had this exact fix via `validating_started_at`;
the Single-mode `rolling_out` path was missing the equivalent anchor.

## User-facing change

A commit/release self-upgrade now gets the full 15-minute rollout window
measured **from the ARM PATCH**, so a healthy new revision is no longer
false-aborted into `failed_rollout` just because the build phase was slow.
Genuine rollout failures (provisioning failed, terminal `running_state`,
replica-zero crash-loop) still escalate immediately as before.

## API / IaC diff summary

- `api/services/upgrade/state.py`: new `rolling_out_started_at` field on
  `UpgradeState` (+ entity (de)serialization). Empty for rows created before
  this field existed.
- `api/tasks/upgrade/pipeline.py`: stamp `rolling_out_started_at = now` in the
  same CAS that moves `patching → rolling_out` (the "ARM PATCH submitted"
  transition).
- `api/tasks/upgrade/reconciler.py`: the `ROLLING_OUT_TIMEOUT_SECONDS` budget
  is anchored to `rolling_out_started_at` (falling back to `started_at` for
  legacy in-flight rows).

## Validation evidence

- `uv run pytest -q api/tests/test_upgrade_task.py api/tests/test_upgrade_chaos.py api/tests/test_upgrade_bluegreen.py api/tests/test_upgrade_revisions.py api/tests/test_upgrade_state.py` → 96 passed.
- New regression tests:
  - `test_reconciler_rolling_out_budget_anchored_to_patch_not_start` — a row
    whose upgrade started 14 min ago but whose PATCH landed 30 s ago is **not**
    aborted while the new revision boots; a PATCH older than the budget still
    fails.
  - `test_reconciler_rolling_out_budget_falls_back_to_started_at` — legacy
    rows with an empty `rolling_out_started_at` still get a stuck-guard via
    `started_at`.
- Live: deployed to the active revision, then a commit-channel self-upgrade
  reached `succeeded` instead of `failed_rollout`.
