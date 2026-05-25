# Container App Consumption Resource Total

## Motivation

The postprovision sidecar swap failed during Azure Container Apps preflight validation because the six-sidecar Consumption template requested `3.25` CPU and `4.5Gi` memory. Consumption revisions require the aggregate CPU and memory across all containers to match one of Azure's supported resource combinations.

## User-facing change

Full deploys can now keep the existing `4.5Gi` aggregate memory request while using the valid `2.25` aggregate CPU request for the six-sidecar Container App revision. If the sidecar swap fails again, `postprovision.sh` now prints the tail of `swap.log` instead of exiting before the useful Azure error is shown.

## API/IaC diff summary

- `infra/modules/containerAppControl.bicep` lowers the `api` and `worker` sidecars from `1.0` CPU each to `0.5` CPU each.
- The six-sidecar total becomes `2.25` CPU / `4.5Gi` memory, matching the Consumption resource pair accepted by Azure Container Apps.
- `scripts/dev/postprovision.sh` temporarily disables `errexit` around the background swap `wait` so non-zero deployment status can be reported with `swap.log` context.

## Validation evidence

- `bash -n scripts/dev/postprovision.sh` passed.
- `az bicep build --file infra/modules/containerAppControl.bicep --stdout` passed with only the existing Bicep upgrade notice.
- Manual sidecar resource check confirmed the six-sidecar total is `2.25` CPU / `4.5Gi` memory.
- `postprovision.sh` redeploy with image tag `20260525021659` passed: all three images built, `ca-swap-20260525021659` reached `Succeeded`, and `/api/health` returned HTTP 200.
- Live Container App template confirmed `api=0.5 CPU / 1Gi`, `worker=0.5 CPU / 1Gi`, `frontend=0.25 CPU / 0.5Gi`, `beat=0.25 CPU / 0.5Gi`, `redis=0.25 CPU / 0.5Gi`, and `terminal=0.5 CPU / 1Gi`.
- Platform Storage network posture was restored after validation: `publicNetworkAccess=Disabled`, `defaultAction=Deny`, `ipRules=[]`.