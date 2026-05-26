# 2026-05-27 — AKS runtime RBAC: default workload Storage target from platform env

## Motivation

A live `elb-cluster-01` (rg-elb-cluster) hit `AKS cache failed · 1/1` on
the warmup card for `16S_ribosomal_RNA`. The terminal sidecar log showed
the underlying `azcopy` call returning:

```
RESPONSE 403: 403 This request is not authorized to perform this operation using this permission.
ERROR CODE: AuthorizationPermissionMismatch
```

Direct verification confirmed the kubelet MI
`c1940a76-33f4-4876-a5b1-e36012f89bd0` held **only** `AcrPull` on
`acrelbdashboard3abp67bppe`. It had no Storage role on the workload
account `stelbdashboard3abp67bppe`. The warmup script
[api/services/warmup/scripts.py](../../../api/services/warmup/scripts.py)
authenticates via `azcopy login --identity` (kubelet IMDS token), so a
missing data-plane RBAC manifests as a 403 on the first manifest HEAD.

Root cause: the SPA cluster-provision form omits `storage_account` in
the request body. The route forwarded `""` to `provision_aks`, which
forwarded `""` to `ensure_aks_runtime_rbac`, where
`if storage_account and storage_resource_group:` silently skipped the
`Storage Blob Data Contributor` grant. The cluster came up "ready" with
half-permissioned RBAC and warmup failed later at runtime.

## User-facing change

* New clusters provisioned through the dashboard always have their
  kubelet identity granted `Storage Blob Data Contributor` on the
  dashboard's own workload Storage account, even when the SPA omits the
  `storage_account` field in the provision request.
* No SPA / UI change. The existing `Re-warm` button works once RBAC
  propagates (~5 minutes).

## API / IaC diff summary

* [api/tasks/azure/rbac.py](../../../api/tasks/azure/rbac.py): new
  `_resolve_workload_storage_defaults(storage_resource_group, storage_account)`
  helper. When the caller omits the workload-Storage target,
  `ensure_aks_runtime_rbac` now defaults to `AZURE_STORAGE_ACCOUNT` /
  `STORAGE_ACCOUNT_NAME` + `AZURE_RESOURCE_GROUP` from the worker env
  (the Container App always carries these). Existing explicit-target
  call sites are unaffected.
* [api/tests/conftest.py](../../../api/tests/conftest.py): autouse
  `_env_baseline` now `delenv`s `AZURE_STORAGE_ACCOUNT`,
  `STORAGE_ACCOUNT_NAME`, `AZURE_RESOURCE_GROUP` so tests that exercise
  the "no storage target" path do not pick up ambient azd env.
* [api/tests/test_azure_tasks.py](../../../api/tests/test_azure_tasks.py):
  added `test_ensure_aks_runtime_rbac_defaults_storage_from_env` as a
  regression guard.

No infra (Bicep) change. The Container App already exports the env vars
the helper reads.

## Validation

* `uv run pytest -q api/tests/test_azure_tasks.py api/tests/test_azure_provision_aks.py`
  → 26 passed.
* `uv run pytest -q api/tests` → 1528 passed.
* `uv run ruff check api/tasks/azure/rbac.py api/tests/test_azure_tasks.py api/tests/conftest.py`
  → clean.

## Live remediation for the existing cluster

The env-default only helps **new** provisions. The already-broken
`elb-cluster-01` was patched directly so the user can `Re-warm` once
Entra propagates:

```bash
az role assignment create \
  --assignee-object-id c1940a76-33f4-4876-a5b1-e36012f89bd0 \
  --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Contributor" \
  --scope /subscriptions/b052302c-4c8d-49a4-aa2f-9d60a7301a80/resourceGroups/rg-elb-dashboard/providers/Microsoft.Storage/storageAccounts/stelbdashboard3abp67bppe
```

Confirmed assignment created. The `POST /api/aks/{cluster}/assign-roles`
task is the dashboard equivalent and will now also pick up the
workload-Storage target from env when the body omits it.
