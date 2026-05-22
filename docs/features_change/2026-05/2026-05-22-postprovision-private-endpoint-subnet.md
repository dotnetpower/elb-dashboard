# Postprovision Private Endpoint Subnet Wiring

## Motivation

Numbered deployments can place the active Container App in a new platform VNet while older Storage accounts still have private endpoints in the previous VNet. The postprovision sidecar swap also omitted `platformPrivateEndpointSubnetId`, so the deployed `api` and `worker` sidecars received an empty `PLATFORM_PRIVATE_ENDPOINT_SUBNET_ID` even though `infra/main.bicep` wires the value correctly.

## User-Facing Change

Fresh and numbered deployments now preserve the private endpoint subnet id during the six-sidecar swap. Runtime storage creation can attach workload Storage private endpoints to the current deployment VNet instead of silently skipping that step.

## API/IaC Diff Summary

- `scripts/dev/postprovision.sh` now requires `CONTAINER_ENV_NAME`, resolves the Container Apps Environment infrastructure subnet, derives the sibling `snet-private-endpoints` subnet id, validates that subnet exists, and passes it to `containerAppControl.bicep` as `platformPrivateEndpointSubnetId`.
- The sidecar environment variables produced by `infra/modules/containerAppControl.bicep` keep using the existing `PLATFORM_PRIVATE_ENDPOINT_SUBNET_ID` contract.

## Validation Evidence

- `bash -n scripts/dev/postprovision.sh`
- Azure diagnosis before the fix: `ca-elb-dashboard-01` ran in `rg-elb-dashboard-01/vnet-elb-dashboard-01`, while the failing Storage account `stelbdashboardogi2vbkece` had approved private endpoints only in `rg-elb-dashboard/vnet-elb-dashboard`.