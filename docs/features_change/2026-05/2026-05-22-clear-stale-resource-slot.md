# Clear Stale Resource Slot

## Motivation

After a numbered deployment, `azd` persists `ELB_RESOURCE_NAME_SLOT=slot01`. If the original `rg-elb-dashboard` is later deleted, a subsequent `./deploy.sh` should return to the default resource group instead of continuing to target `rg-elb-dashboard-01` from stale environment state.

## User-Facing Change

`./deploy.sh` now clears a persisted numbered slot when `rg-elb-dashboard` is available. Explicit operator overrides through `ELB_RESOURCE_NAME_SLOT` or `ELB_RESOURCE_NAME_SUFFIX` are still honored.

## API/IaC Diff Summary

- Updated `deploy.sh` to inspect the default resource group before reusing `ELB_RESOURCE_NAME_SLOT` from the selected `azd` environment.
- If the default group does not exist or is empty, the script exports an empty slot so `resolve-resource-group.sh` persists the default `rg-elb-dashboard` target.

## Validation Evidence

- `bash -n deploy.sh`
- `git diff --check -- deploy.sh docs/features_change/2026-05/2026-05-22-clear-stale-resource-slot.md`