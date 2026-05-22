# Deploy Progress UI

## Motivation

`./deploy.sh` and the `azd` hooks already expose a deployment step map, but the plain bracketed output is hard to scan during long deployments.

## User-Facing Change

Deployment progress output now uses a compact, colored, ASCII status style when stdout is interactive. It falls back to plain text automatically for CI, non-TTY output, `NO_COLOR=1`, or `ELB_DEPLOY_STYLE=plain`.

## API/IaC Diff Summary

- Updated `scripts/dev/azd-progress.sh` only; existing `plan`, `step`, `done`, and `note` commands remain compatible with `deploy.sh`, `azure.yaml`, and `postprovision.sh`.
- Added color helpers for running, done, note, and error markers without changing deployment behavior.

## Validation Evidence

- `bash -n scripts/dev/azd-progress.sh`
- `scripts/dev/azd-progress.sh plan`
- `scripts/dev/azd-progress.sh step 0 "Local bootstrap" "checking Azure CLI and azd auth"`
- `scripts/dev/azd-progress.sh done 0 "Local bootstrap"`
- `NO_COLOR=1 scripts/dev/azd-progress.sh plan`