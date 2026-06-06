---
title: Self-upgrade reconciler — recognize ACA "RunningAtMaxScale" as healthy
description: A healthy new revision pinned at minReplicas==maxReplicas==1 reports
  runningState "RunningAtMaxScale", not "Running". The reconciler health check
  only accepted "Running", so it classified every healthy upgrade as "booting"
  forever and the rollout budget failed an upgrade that actually succeeded.
tags:
  - operate
  - deployment-reference
---

# Self-upgrade reconciler — recognize "RunningAtMaxScale" as healthy

## Motivation

Four consecutive commit-channel self-upgrades each built all three images,
brought up a new revision that was `Active / RunningAtMaxScale / Provisioned`,
yet ended in `failed_rollout` with `rolling_out exceeded budget (~960s)`. The
preceding fixes — success-via-deployed-image-tag, PATCH-anchored budget, and
success-before-budget ordering — were all necessary but none of them made an
upgrade reach `succeeded`.

## Root cause

`_green_health` (and the blue/green `_is_healthy`) classified a revision as
healthy only when `running_state.lower() == "running"`. But Azure Container
Apps reports a healthy, serving revision that is pinned at
`minReplicas == maxReplicas == 1` — exactly this project's topology — with
`runningState = "RunningAtMaxScale"`, not `"Running"`. So a perfectly healthy
new revision was classified `booting` on every reconcile tick, never reached
the success branch, and the (correctly PATCH-anchored) rollout budget
eventually failed it.

The unit tests masked the bug because `_FakeWatcher` was constructed with
`running="Running"`, a value the live control plane never returns for a pinned
single-replica revision.

This was confirmed by instrumenting the reconcile decision and reading the
worker logs: `image_matches=True` and the revision was Provisioned, but the
health classification returned `booting` because the running state was
`RunningAtMaxScale`.

## User-facing change

A commit/release self-upgrade now reaches `succeeded` once the new revision is
serving, because the health check accepts any `running*` running state
(`Running`, `RunningAtMaxScale`, …) together with `Provisioned`. Genuine
failures (provisioning failed, terminal `running_state`, replica-zero
crash-loop) still escalate immediately.

## API / IaC diff summary

- `api/tasks/upgrade/reconciler.py`:
  - new `_RUNNING_STATE_HEALTHY_PREFIX = "running"`; `_green_health`, the
    Single-mode fall-through readiness check, all use
    `running_state.lower().startswith("running")` instead of `== "running"`.
  - added two decisive INFO diagnostics (the success/budget decision inputs and
    the resolved revision health) so a future stuck `rolling_out` row can be
    diagnosed from the worker logs without another guess-and-redeploy cycle.
- `api/services/upgrade/rollout_watcher.py`: `_is_healthy` accepts any
  `running*` state (same latent bug on the blue/green wait path).

## Validation evidence

- `uv run pytest -q api/tests/test_upgrade_task.py api/tests/test_upgrade_chaos.py api/tests/test_upgrade_bluegreen.py api/tests/test_upgrade_revisions.py api/tests/test_upgrade_state.py api/tests/test_upgrade_rollout_watcher.py` → all green.
- New regression test `test_reconciler_succeeds_with_running_at_max_scale` —
  `_FakeWatcher(running="RunningAtMaxScale", provisioning="Provisioned")` now
  drives the row to `succeeded` (would fail as `booting`/budget before the fix).
- Live: deployed to the active revision, then a commit-channel self-upgrade
  reached `succeeded`.
