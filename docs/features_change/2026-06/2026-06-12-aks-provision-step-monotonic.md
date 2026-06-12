---
title: AKS provisioning step counter no longer goes backwards (4/5 → 3/5)
description: Pin the pre-create dashboard-MI self-grant ticks to the RG step so the cluster provisioning banner step counter stays monotonic.
tags:
  - blast
  - ui
---

# AKS Provisioning — Monotonic Step Counter

## Motivation

During cluster creation the provisioning banner step indicator jumped
**4/5 → 3/5** mid-way through, looking like the progress had regressed.

The provision flow defines five ordered steps in
[api/tasks/azure/provision.py](../../../api/tasks/azure/provision.py)
`_PROVISION_STEPS`:

1. `creating_cluster` (1/5)
2. `ensuring_resource_group` (2/5)
3. `arm_create_or_update` (3/5)
4. `ensuring_rbac` (4/5)
5. `completed` (5/5)

The **pre-create dashboard-MI self-grant** runs *before* the ARM create
(step 3), inside the RG-preparation window. It reuses the
`_RBAC_SUB_PHASES` strings (e.g. `ensuring_dashboard_mi_rbac`), which the
`_publish` wrapper maps to the parent `ensuring_rbac` step (4). So the
banner published `1 → 2 → 4 → 3 (ARM) → 4 → 5`, surfacing as the visible
**4/5 → 3/5** regression.

## User-facing change

The cluster provisioning banner step counter now increases monotonically
(`1/5 → 2/5 → 3/5 → 4/5 → 5/5`). The pre-create self-grant ticks render
under **Step 2/5 · Ensuring resource group** with their specific message
(e.g. "Self-granting dashboard MI on cluster RG") instead of jumping to
step 4 and then back to 3.

## API / IaC diff summary

* `api/tasks/azure/provision.py`
  * `_publish` gains an optional `step_override: int | None` parameter that
    pins the published step regardless of the phase's natural step.
  * `_pre_create_rbac_progress` passes
    `step_override=_STEP_INDEX["ensuring_resource_group"]` (= 2) so the
    pre-ARM self-grant ticks stay on the RG step.
  * The post-create RBAC self-grant (`_rbac_progress`) is unchanged — it
    correctly stays at step 4 because it runs after ARM (step 3).

No IaC, route, or response-schema changes. The frontend renders whatever
`step` the task publishes, so no `web/` change is required.

## Validation evidence

* New regression test
  `test_provision_aks_step_counter_is_monotonic_with_pre_create_rbac` in
  [api/tests/test_azure_provision_aks.py](../../../api/tests/test_azure_provision_aks.py)
  fires the pre-create RBAC progress callback and asserts (a) the pre-ARM
  `ensuring_dashboard_mi_rbac` tick lands on step 2 and (b) every published
  step is monotonically non-decreasing.
* `uv run pytest -q api/tests/test_azure_provision_aks.py` → 20 passed.
* `uv run pytest -q api/tests/test_azure_tasks.py api/tests/test_rbac_preflight.py`
  → 50 passed (shared sub-phase step mapping unaffected).
* `uv run ruff check api/tasks/azure/provision.py api/tests/test_azure_provision_aks.py`
  → clean.
