# ACR Build Access Guard

## Motivation

Fast sidecar deploys and full postprovision builds both run `az acr build` from
Microsoft-managed ACR Tasks agents. When the registry is restored to private
networking after deployment, those agents need a temporary, verified build
window instead of repeated manual firewall retries.

## User-Facing Change

The deployment scripts now open the platform ACR build policy in one shared
flow, wait for the policy to become visible, settle briefly for build-agent
propagation, and restore the original registry network policy after the build.
This applies to both full postprovision builds and `quick-deploy.sh` sidecar
updates.

## API / IaC Diff Summary

- Added `scripts/dev/acr-build-access.sh` as the shared ACR build-access guard.
- Updated `scripts/dev/postprovision.sh` to use the guard instead of inline ACR
  public-network toggling.
- Updated `scripts/dev/quick-deploy.sh` so fast deploys use the same verified
  open/build/restore lifecycle.
- Workload Storage network isolation remains unchanged.

## Validation Evidence

- `bash -n scripts/dev/acr-build-access.sh scripts/dev/quick-deploy.sh scripts/dev/postprovision.sh`
- `scripts/dev/quick-deploy.sh frontend 20260519212803`
- `az acr show -g rg-elb-ca -n acrelbnm5virmqrdi5c --query '{public:publicNetworkAccess,defaultAction:networkRuleSet.defaultAction,trusted:networkRuleBypassOptions}' -o json`
- `curl https://ca-elb-control.gentlemeadow-01289e5b.koreacentral.azurecontainerapps.io/api/health`