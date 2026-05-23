# Local Storage RBAC Recovery

## Motivation

Local host-mode debugging uses the developer's [Azure CLI](https://learn.microsoft.com/cli/azure/) identity for [Azure Storage](https://learn.microsoft.com/azure/storage/common/storage-introduction) data-plane calls. When that identity lacks Storage Blob Data roles, the BLAST database manager correctly degrades with `access_denied`, but the UI did not provide a direct recovery action.

## User-facing change

The BLAST Databases section and modal now show a local-only **Grant local RBAC** action when Storage returns `access_denied` from a local API process. The action assigns the local API Azure credential principal the local-debug Storage roles and prompts the operator to wait for [Azure RBAC](https://learn.microsoft.com/azure/role-based-access-control/overview) propagation.

`deploy.sh` also attempts to grant the deployer local-debug RBAC after `azd up` so a fresh deployment can be debugged from host mode without a separate manual RBAC step. Set `ELB_SKIP_LOCAL_RBAC=true` to skip this post-deploy helper.

## API / IaC diff summary

- Added `POST /api/storage/local-debug/grant-rbac`, guarded to local API processes only.
- Hardened the grant path so it requires real local MSAL auth, rejects browser/API user mismatches for user credentials, validates the resolved Azure credential `oid`, and avoids returning full Azure scopes or principal IDs to the browser.
- Added `api.services.storage.local_rbac.grant_local_debug_storage_roles` to assign Storage Blob Data Contributor, Storage Table Data Contributor, and Storage Account Contributor at the workload Storage account scope.
- No IaC changes; the deployed shared managed identity RBAC remains owned by Bicep.

## Validation evidence

- `uv run pytest -q api/tests/test_storage_local_rbac.py api/tests/test_storage_public_access.py api/tests/test_storage_data.py`
- `bash -n deploy.sh`
- `cd web && npm run build`