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
had two compounding defects:

1. **Wrong anchor.** It computed elapsed time from `row.started_at` — the
   moment the **whole** upgrade began (`queued`). But `started_at` already
   absorbs the entire clone + 3× `az acr build` phase (~10-13 min), leaving
   only ~2 min of the 15-min (`ROLLING_OUT_TIMEOUT_SECONDS`) budget for the
   new revision to pull its image and pass readiness.

2. **Wrong order (the decisive bug).** The budget check ran *before* the
   success/health check. In ACA Single mode the revision swap tears down the
   beat that was reconciling, and the new beat (on the swapped-in revision)
   only starts after redis + the beat schedule rebuild. Its **first** reconcile
   can therefore fire >15 min after the PATCH even though the new revision came
   up healthy within minutes — and because the budget check ran first, it
   failed an upgrade that had already succeeded. Two consecutive live runs
   ended in `failed_rollout` ("exceeded budget 985s / 958s") while the new
   revision `…--v0-2-0-commit-…` was `Active / RunningAtMaxScale / Provisioned`.

The blue/green path already had the anchor fix via `validating_started_at`;
the Single-mode `rolling_out` path was missing both the equivalent anchor and
the success-before-budget ordering.

## User-facing change

A commit/release self-upgrade now:

- evaluates **success first** — a healthy revision whose deployed api image is
  tagged for the target version is marked `succeeded` regardless of how long
  ago the PATCH landed (so a slow post-swap beat start no longer false-aborts
  it); and
- applies the 15-minute rollout budget **only while the revision is genuinely
  still booting** (or its image never deployed), anchored to the ARM PATCH
  moment rather than the overall upgrade start.

Genuine rollout failures (provisioning failed, terminal `running_state`,
replica-zero crash-loop) still escalate immediately as before.

## API / IaC diff summary

- `api/services/upgrade/state.py`: new `rolling_out_started_at` field on
  `UpgradeState` (+ entity (de)serialization). Empty for rows created before
  this field existed.
- `api/tasks/upgrade/pipeline.py`: stamp `rolling_out_started_at = now` in the
  same CAS that moves `patching → rolling_out` (the "ARM PATCH submitted"
  transition).
- `api/tasks/upgrade/reconciler.py`: the success/health classification now runs
  **before** the stuck-budget guard; the budget (anchored to
  `rolling_out_started_at`, falling back to `started_at` for legacy rows) only
  fires when the revision is still booting or the target image is not yet
  deployed.

## Validation evidence

- `uv run pytest -q api/tests/test_upgrade_task.py api/tests/test_upgrade_chaos.py api/tests/test_upgrade_bluegreen.py api/tests/test_upgrade_revisions.py api/tests/test_upgrade_state.py` → 97 passed.
- New regression tests:
  - `test_reconciler_healthy_revision_over_budget_still_succeeds` — a healthy,
    correctly-tagged revision whose PATCH landed 20 min ago is marked
    `succeeded`, not `failed_rollout` (the actual production bug).
  - `test_reconciler_rolling_out_budget_anchored_to_patch_not_start` — a row
    whose upgrade started 14 min ago but whose PATCH landed 30 s ago is **not**
    aborted while the new revision boots; a still-booting revision whose PATCH
    is older than the budget still fails.
  - `test_reconciler_rolling_out_budget_falls_back_to_started_at` — legacy
    rows with an empty `rolling_out_started_at` still get a stuck-guard via
    `started_at`.
- Live: deployed to the active revision, then a commit-channel self-upgrade
  reached `succeeded` instead of `failed_rollout`.

