---
title: Self-upgrade reconciler — detect success via deployed image, not in-proc version
description: Mark a self-upgrade succeeded when the deployed ACA api image is tagged for the target version, so the reconciler running on the old revision no longer wedges at 95% until the rollout budget fails an upgrade that actually worked.
tags:
  - operate
  - deployment-reference
---

# Self-upgrade reconciler — detect success via deployed image

## Motivation

A self-upgrade whose images built and deployed successfully (new revision
`…--v0-2-0-commit-194ee1d-…` live and `RunningAtMaxScale`, SPA showing the new
version) still ended in `failed_rollout` with
`rolling_out exceeded budget (970s)`. The upgrade had actually worked, but the
state machine reported failure.

## Root cause

`reconcile_rolling_out_inline` only marked the row `succeeded` when
`_api.__version__ == target_version`. `_api.__version__` is **this process's**
version, and the reconciler runs in the **beat** sidecar. In ACA Single mode the
beat can keep running on the **old** revision after the new revision is already
serving, so `__version__` never flips to the target — the row stays at 95%
"awaiting readiness probe" until the 15-minute stuck-guard fails it.

## User-facing change

A self-upgrade is now marked `succeeded` as soon as the deployed ACA api image
is tagged for the target version (authoritative regardless of which revision the
reconciler runs in), provided the new revision is healthy. Genuine failures
(provisioning failed, terminal running_state, replica-zero crash) still escalate
to `failed_rollout` with a specific diagnostic.

## API / IaC diff summary

- `api/tasks/upgrade/reconciler.py` `reconcile_rolling_out_inline`:
  - The success gate now fires on `version_matches OR image_matches`, where
    `image_matches` reads `aca.read_current_images().api` and compares its tag
    to `v<target_version>` via the existing `_image_matches_version`.
  - When the gate fires it classifies the new revision with `_green_health`
    (healthy | booting | failed). `failed` → `_fail_rollout` with the specific
    reason (0 replicas / provisioning state / running_state); `booting` → defer
    at 95%; `healthy` → `succeeded`, stamping `running_version` with the proven
    version (this process's version if it matched, else the target).
- No infra change.

## Validation evidence

- Live root cause: an end-to-end successful upgrade (api+frontend+terminal built,
  ARM PATCH applied, new revision `RunningAtMaxScale`, SPA at
  `v0.2.0-commit.194ee1d`) was reported `failed_rollout` "rolling_out exceeded
  budget (970s)".
- New test `test_reconciler_succeeds_on_image_match_when_version_lags`:
  `__version__` pinned to the old version, deployed image at the target →
  `succeeded`, `running_version == target`.
- Existing hard-failure tests preserved (degraded running_state, replica-zero)
  with their specific diagnostics intact.
- `uv run pytest -q api/tests/test_upgrade_*` → 83 passed.
- `uv run ruff check api/tasks/upgrade/reconciler.py` → clean.
