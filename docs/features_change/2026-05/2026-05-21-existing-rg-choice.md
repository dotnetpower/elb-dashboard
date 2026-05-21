# Existing resource group choice

## Motivation

The deployment now uses the fixed default group `rg-elb-dashboard`. If that group already exists and contains resources, a fresh `azd up` needs an explicit operator decision instead of silently mixing old and new resources.

## User-facing change

- `azd up` and `./deploy.sh` detect when `rg-elb-dashboard` already contains resources.
- Interactive runs show the resource count and a small sample, then ask whether to delete the group and continue, keep it and deploy to the next numbered group such as `rg-elb-dashboard-01`, or abort.
- Non-interactive automation can set `ELB_EXISTING_RG_ACTION=delete|number|abort`.
- Choosing numbering stores the azd-safe `ELB_RESOURCE_NAME_SLOT=slot01` value, which Bicep converts to the visible `-01` resource-name suffix.

## API / IaC diff summary

- Added `scripts/dev/resolve-resource-group.sh` for resource-group collision detection and action selection.
- Added `resourceNameSlot` to `infra/main.bicep` and `infra/main.parameters.json`.
- Updated generated names so suffixes apply consistently to `rg-elb-dashboard-01`, `vnet-elb-dashboard-01`, `ca-elb-dashboard-01`, `id-elb-dashboard-01-*`, `acrelbdashboard01*`, `stelbdashboard01*`, and related resources.
- Updated `azure.yaml`, `deploy.sh`, and `scripts/dev/azd-progress.sh` to include the resource-group choice before Bicep provisioning.

## Validation evidence

- `az bicep build --file infra/main.bicep --outfile infra/main.json`
- `bash -n scripts/dev/resolve-resource-group.sh deploy.sh scripts/dev/postprovision.sh scripts/dev/azd-progress.sh scripts/dev/register-providers.sh`
- `ELB_EXISTING_RG_ACTION=number ./scripts/dev/resolve-resource-group.sh --subscription 577d6332-de48-4a30-be66-dded26a712ea --environment elb-dashboard` -> detected populated `rg-elb-dashboard`, selected `rg-elb-dashboard-01`, and persisted `ELB_RESOURCE_NAME_SLOT=slot01` without deleting anything.
- `ELB_RESOURCE_NAME_SLOT=slot01` + `azd provision --preview --environment elb-dashboard --no-prompt` -> previewed `rg-elb-dashboard-01`, `ca-elb-dashboard-01`, `acrelbdashboard01*`, and `stelbdashboard01*` creation.
- Empty `ELB_RESOURCE_NAME_SLOT` + `azd provision --preview --environment elb-dashboard --no-prompt` -> previewed the default `rg-elb-dashboard` deployment path successfully.