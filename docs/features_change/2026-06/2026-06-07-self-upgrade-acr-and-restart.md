---
title: Self-upgrade fixes — PLATFORM_ACR_NAME env and failed_pre restart
description: Fix the self-upgrade az acr build failure (missing PLATFORM_ACR_NAME on the worker sidecar) and the stuck failed_pre state that blocked restarting after any pre-build failure.
tags:
  - operate
  - deployment-reference
---

# Self-upgrade fixes — PLATFORM_ACR_NAME env and failed_pre restart

## Motivation

Starting a self-upgrade from the dashboard failed immediately with:

```
az acr build api failed: PLATFORM_ACR_NAME is not set; cannot run az acr build
```

The upgrade row went to `failed_pre` and could not be restarted from the UI:
the Start button was enabled but every retry returned HTTP 409
`upgrade already in progress (state=failed_pre)`.

## Root causes (two distinct bugs)

1. **Missing env on the worker.** The self-upgrade pipeline runs as a Celery
   task on the **worker** sidecar. Its image builder
   (`api.services.upgrade.image_builder._acr_name()`) reads `PLATFORM_ACR_NAME`
   from the environment, but that variable was only defined on the **terminal**
   sidecar in `infra/modules/containerAppControl.bicep`. The api / worker / beat
   sidecars never received it, so `az acr build` aborted in pre-flight.

2. **`failed_pre` was a stuck terminal state.** `start_upgrade_inline` only
   accepted the CAS transition `idle → queued`. Once the row was in `failed_pre`
   (or any terminal failure / success state) the start was refused with 409,
   and there was **no reset path** anywhere in the app. The SPA's Start button
   is enabled for the `failed` / `rolled_back` phases, so the user could click
   it but always got 409 — the upgrade flow was permanently wedged after a
   single pre-build failure.

## User-facing change

- The self-upgrade `az acr build` step now finds the platform ACR and proceeds.
- A failed or completed upgrade can be restarted from the dashboard. `start`
  now accepts a fresh upgrade from any **non-active** state (`idle`,
  `failed_pre`, `failed_rollout`, `rolled_back`, `rollback_failed`,
  `succeeded`). Genuinely in-flight states (`queued`, `fetching`, `building`,
  `patching`, `rolling_out`, `validating`, `confirming`, `rolling_back`) still
  block a concurrent start with 409.

## API / IaC diff summary

- `infra/modules/containerAppControl.bicep` (+ compiled `.json`): add
  `PLATFORM_ACR_NAME` (value `platformAcrName`) to the api, worker, and beat
  sidecar `env` arrays. The terminal sidecar already had it.
- `api/tasks/upgrade/pipeline.py`: add `_RESTARTABLE_START_STATES` and
  `_cas_start_from_restartable()`; `start_upgrade_inline` now tries the
  `→ queued` CAS from each restartable state (idle first) instead of only
  `idle → queued`. The 409-on-active-state contract is unchanged.
- No response-shape change; `UpgradeStartRefused` / 409 semantics preserved for
  in-flight states.

## Validation evidence

- Live root-cause confirmation: `GET /api/upgrade/status` →
  `state=failed_pre, phase_detail="… PLATFORM_ACR_NAME is not set …"`;
  `POST /api/upgrade/start` → `409 "upgrade already in progress
  (state=failed_pre)"`.
- Live env remediation applied to the running revision (api/worker/beat now
  carry `PLATFORM_ACR_NAME=acrelbdashboard3abp67bppe`), verified via
  `az containerapp revision show … env[?name=='PLATFORM_ACR_NAME']`.
- New regression tests in `api/tests/test_upgrade_task.py`:
  `test_start_recovers_from_failed_pre`,
  `test_start_recovers_from_any_terminal_state` (parametrised over all five
  terminal states), and `test_start_refused_while_active` (parametrised over all
  eight active states).
- `uv run pytest -q api/tests` → 3036 passed, 3 skipped.
- `uv run ruff check` on touched files → clean.
