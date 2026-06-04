---
title: Native ACA blue/green self-upgrade with guaranteed rollback
description: STRICT_BLUEGREEN flag adds a staged green revision, confirm-window
  traffic flip rollback, and revision garbage collection to the in-app upgrade
  flow. Default OFF preserves the legacy Single-mode recreate.
tags:
  - operate
  - release
---

# Native ACA blue/green self-upgrade

## Motivation

The in-app self-upgrade flow recreated the single Container App revision in
place. That gave two weak guarantees the operator actually cares about:

1. **Rollback was slow and not guaranteed.** Reverting meant re-PATCHing the
   previous images, which re-pulls from ACR and reboots every sidecar — minutes,
   and impossible if ACR/the old image is unreachable.
2. **No clean-up contract.** Nothing pruned superseded revisions.

The user's two hard requirements were: rollback **must** be guaranteed if a
problem appears after the update, and a successful update **must not** leave
garbage containers behind. Native [Azure Container Apps](https://learn.microsoft.com/azure/container-apps/revisions)
blue/green (multiple-revision mode + traffic weights) is the orthodox way to get
both.

## User-facing change

When `STRICT_BLUEGREEN=true`:

- An update stages a **green** revision at 0% traffic, health-checks it
  (`validating`), then cuts traffic over and holds a **confirm window**
  (`confirming`, default 300 s) with the previous **blue** revision kept warm at
  weight 0.
- **Rollback during the confirm window is a traffic-weight flip back to the warm
  blue revision — seconds, no ACR pull, no reboot.** The Upgrade page detects
  this (`fastFlip`) and tells the operator the fast path is available, and the
  "Roll back" button is no longer gated behind the ACR snapshot preflight.
- On confirm, the superseded blue revision is **garbage-collected**, keeping at
  most `UPGRADE_REVISION_KEEP_N` (default 2) inactive revisions.
- The Upgrade page surfaces the new `validating` / `confirming` states and the
  `green_revision` / `blue_revision` / `confirm_deadline` / `traffic_serving`
  fields.

When `STRICT_BLUEGREEN` is OFF (default) the legacy Single-mode in-place recreate
runs unchanged — **zero regression**.

## API / IaC diff summary

- `api/services/upgrade/state.py` — added `green_revision`, `blue_revision`,
  `confirm_deadline`, `traffic_serving` dataclass fields (default `""`),
  serialized via `asdict` in `to_public_dict`.
- `api/services/upgrade/revisions.py` (new) — list/flip-traffic/GC helpers over
  the ACA revisions API.
- `api/tasks/upgrade/revision_gc.py` (new) — keep-N revision pruning.
- `api/tasks/upgrade/pipeline.py` — green staging + `validating` entry; timeline
  registers `STATE_VALIDATING` / `STATE_CONFIRMING`.
- `api/tasks/upgrade/reconciler.py` — drives `validating → confirming →
  succeeded`, degraded-green flip-back, post-confirm GC.
- `api/tasks/upgrade/rollback.py` — operator rollback now allowed from
  `confirming` (the highest-value manual-revert moment) via the fast
  traffic-flip path, falling back to snapshot re-PATCH when blue was torn down.
- `web/src/api/upgrade.ts` — added the two states + four staging fields to the
  types.
- `web/src/pages/UpgradePage.tsx` — confirm-window fast-flip rollback copy and
  un-gated button.
- `infra/modules/containerAppControl.bicep` — `STRICT_BLUEGREEN=false`
  registered on the **api / worker / beat** sidecars (Charter §12a Rule 4,
  default-OFF guard).

## Deferred: `activeRevisionsMode` flip (operator action required)

`STRICT_BLUEGREEN=true` only works when the Container App runs in
`activeRevisionsMode: 'Multiple'`. That flip is **intentionally not applied** in
this change because it is provision-irreversible and carries two hazards:

1. **Regression guard** — Multiple mode with `STRICT_BLUEGREEN` still OFF gives
   each new revision 0% traffic by default, so the legacy in-place recreate
   would silently take no traffic. The two switches must be flipped **together**.
2. **IaC vs runtime traffic ownership** — in Multiple mode the reconciler mutates
   the `traffic` array at runtime. A declarative `traffic` block would be reset
   by the next `azd provision`, which during a confirm window or rollback would
   yank traffic to the wrong revision. The cutover stays runtime-owned (no static
   traffic block) and operators must avoid `azd provision` mid-cutover.

The rollout is therefore a coordinated manual deploy: set
`activeRevisionsMode: 'Multiple'` **and** `STRICT_BLUEGREEN=true` in the same
revision, no static `traffic` block. Optional knobs:
`UPGRADE_CONFIRM_WINDOW_SECONDS` (300), `UPGRADE_VALIDATING_TIMEOUT_SECONDS`
(600), `UPGRADE_REVISION_KEEP_N` (2). The caveat is documented inline at the
`activeRevisionsMode` line in `infra/modules/containerAppControl.bicep`.

## Validation evidence

- `uv run pytest -q api/tests` → **2637 passed, 3 skipped**.
- Blue/green suite `api/tests/test_upgrade_bluegreen.py` → **14 passed**,
  including `test_operator_rollback_during_confirm_window_flips_to_blue`,
  `test_confirming_green_degraded_flips_back_to_blue`, and
  `test_confirming_succeeds_after_deadline_and_gcs_blue`.
- New GC / revisions suites `test_upgrade_revision_gc.py`,
  `test_upgrade_revisions.py` pass.
- Timeline invariant `test_state_transition_timeline_walks_through_every_state`
  asserts both new states stay registered.
- `cd web && npm run build` succeeded; `npm test -- --run` → 616 passed.
- `uv run ruff check api` → clean.
- `az bicep build --file infra/modules/containerAppControl.bicep` → exit 0.
