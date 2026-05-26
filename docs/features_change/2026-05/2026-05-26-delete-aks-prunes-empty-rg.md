# Delete AKS prunes empty resource group

## Motivation

Deleting an AKS cluster from the dashboard removed the managed cluster but left
the enclosing resource group behind (`rg-elb-cluster` observed on the dashboard
after a successful delete). Over time these empty shells accumulate, clutter the
Resource Groups card, and require manual cleanup in the Azure portal.

## User-facing change

When a user clicks "Delete" on an AKS cluster:

1. The AKS managed cluster is deleted as before (`managed_clusters.begin_delete`,
   which also tears down the auto-managed `MC_*` node-infra RG).
2. After the AKS LRO completes, the enclosing resource group is inspected.
   * If it contains **no remaining resources**, the RG is deleted too.
   * If it still holds Storage / ACR / Key Vault / etc., the RG is left
     untouched (shared-RG safety).
3. The Celery task result now reports `resource_group_status`
   (`deleted` / `retained` / `error`) and `resource_group_remaining` so the UI
   and audit log can surface what happened.

RG cleanup failures (ARM hiccup, missing Reader permission, etc.) are logged at
WARNING and do **not** fail the delete task — the cluster removal already
succeeded.

## API / IaC diff summary

- [api/tasks/azure/lifecycle.py](../../../api/tasks/azure/lifecycle.py):
  `delete_aks` now lists resources in the parent RG via
  `resource_client(...).resources.list_by_resource_group(rg)` and calls
  `resource_groups.begin_delete(rg)` when the list is empty. Task return shape
  gained `resource_group`, `resource_group_status`, `resource_group_remaining`.
- No route, IaC, or schema changes — the existing `/api/aks/{rg}/{name}/delete`
  endpoint dispatches the same Celery task, so the new behaviour is transparent
  to the SPA.

## Validation evidence

- `uv run ruff check api/tasks/azure/lifecycle.py api/tests/test_azure_tasks.py`
  → All checks passed.
- `uv run pytest -q api/tests/test_azure_tasks.py -k delete_aks`
  → 3 passed:
  * `test_delete_aks_removes_empty_resource_group`
  * `test_delete_aks_keeps_resource_group_with_other_resources`
  * `test_delete_aks_rg_cleanup_failure_does_not_fail_task`
- Full suite `uv run pytest -q api/tests/test_azure_tasks.py` → 13 passed.
