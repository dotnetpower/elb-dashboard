# AKS Runtime RBAC Ensure

## Motivation

Warmup jobs need the AKS kubelet identity to pull ElasticBLAST images from ACR and read BLAST database shard files from the Storage account. A freshly created AKS cluster could previously finish provisioning while still missing one of those runtime permissions, causing warmup pods to fail later with image pull or Storage authorization errors.

## User-Facing Change

AKS provisioning now performs the same runtime RBAC ensure that warmup already performs. After the cluster ARM create/update completes, the backend checks the provided ACR and Storage settings and best-effort grants the kubelet identity AcrPull and Storage Blob Data Reader. The manual assign-roles action now covers both roles as well.

## API / IaC Diff Summary

- `api.tasks.azure.provision_aks` now runs an `ensuring_rbac` phase after AKS creation and returns `roles_assigned` / `roles_failed` in the task output.
- `api.tasks.azure.assign_aks_roles` now uses the same shared RBAC ensure helper and accepts `storage_resource_group` plus `storage_account`.
- `/api/aks/{cluster_name}/assign-roles` now passes Storage fields through to the Celery task.
- No IaC changes.

## Validation Evidence

- `uv run ruff format api/tasks/azure.py api/routes/stubs.py api/tests/test_azure_tasks.py api/tests/test_warmup_route.py` left the touched files formatted.
- `uv run ruff check api/tasks/azure.py api/routes/stubs.py api/tests/test_azure_tasks.py api/tests/test_warmup_route.py --select F821,F401,RUF012` passed.
- `uv run pytest -q api/tests/test_azure_tasks.py api/tests/test_warmup_route.py` passed: 10 tests.