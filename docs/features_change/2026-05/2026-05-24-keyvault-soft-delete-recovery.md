# Key Vault Soft-Delete Recovery

## Motivation

A failed numbered deployment can leave the deterministic Key Vault name in Azure soft-delete. Because Key Vault purge protection is enabled, the next `azd up` cannot recreate the same name and fails during Bicep provisioning with `ConflictError: A vault with the same name already exists in deleted state`.

## User-facing change

`./deploy.sh` / `azd up` now recover compatible soft-deleted Key Vaults before Bicep starts. The recovery is limited to deleted vaults whose original vault id points at the selected target resource group and whose tags identify the same `elb-dashboard` azd environment and `role=secrets`.

## API / IaC diff summary

- Added `scripts/dev/recover-deleted-keyvault.sh`.
- Added a preprovision hook step after resource-group selection and before Bicep provisioning.
- The live failed vault `kv-elb-dashboard-01-mul5` was recovered into `rg-elb-dashboard-01` so the next deployment retry can continue instead of colliding with soft-delete.

## Validation evidence

- `az keyvault recover --name kv-elb-dashboard-01-mul5 --resource-group rg-elb-dashboard-01 --location koreacentral` returned the vault to `Succeeded` state.
- `bash -n scripts/dev/recover-deleted-keyvault.sh scripts/dev/postprovision.sh deploy.sh scripts/dev/resolve-resource-group.sh`
- `ELB_RESOURCE_NAME_SLOT=slot01 scripts/dev/recover-deleted-keyvault.sh --environment elb-dashboard --subscription 577d6332-de48-4a30-be66-dded26a712ea` returned `No compatible soft-deleted Key Vault found for rg-elb-dashboard-01` after the live recovery, proving the helper is idempotent.