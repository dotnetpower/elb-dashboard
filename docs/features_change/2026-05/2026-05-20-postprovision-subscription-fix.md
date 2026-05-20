# postprovision.sh: Fix wrong subscription in az acr build

**Date**: 2026-05-20  
**Type**: fix

## Motivation

When `scripts/dev/postprovision.sh` was run directly (e.g. after a failed `azd` hook, or with
`source .env && bash ./scripts/dev/postprovision.sh`), all `az` CLI calls (including `az acr show`
inside `acr-build-access.sh` and `az acr build`) used the globally active subscription from
`~/.azure/` instead of the `AZURE_SUBSCRIPTION_ID` exported from the azd environment.

This caused the following error when deploying to a non-default subscription (e.g. the DEMO
subscription `577d6332`):

```
ERROR: The resource with name 'acrelb4fyfo2zjsub4i' and type 'Microsoft.ContainerRegistry/registries'
could not be found in subscription 'ME-MngEnvMCAP132261-moonchoi-1 (b052302c-...)'.
```

## Change

Added an explicit `az account set --subscription "$AZURE_SUBSCRIPTION_ID"` near the top of
`scripts/dev/postprovision.sh`, immediately before sourcing the helper scripts
(`acr-build-access.sh`, `terminal-base-image.sh`).  This guarantees every `az` call in the script
and its sourced helpers uses the correct subscription regardless of the global az CLI context.

**File changed**: `scripts/dev/postprovision.sh` — 7 lines added after `REPO_ROOT=…`.

## Validation

Full `postprovision.sh` run against DEMO subscription `577d6332` (env: `elb-demo`):

```
[14:19:49] ==> Postprovision starting   RG: rg-elb-demo   ACR: acrelb4fyfo2zjsub4i
[14:20:01] ==> Reusing terminal toolchain base
[14:21:57]     ✓ elb-terminal finished (rc=0)
[14:22:12]     ✓ elb-frontend finished (rc=0)
[14:23:03]     ✓ elb-api finished (rc=0)
[14:23:57] ==> Container App updated to six-sidecar layout
[14:24:41]     ✓ /api/health → 200 OK (attempt 5)
✓ Deployment OK.
  URL: https://ca-elb-control.nicetree-f29b62c5.eastus.azurecontainerapps.io
  RG:  rg-elb-demo
```

`/api/health` → **HTTP 200** confirmed post-deployment.
