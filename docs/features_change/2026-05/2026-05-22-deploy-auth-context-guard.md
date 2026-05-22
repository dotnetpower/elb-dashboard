# Deploy Auth Context Guard

## Motivation

`./deploy.sh` used the active Azure CLI account as the deployment target and then prompted for `azd auth login` when `azd` was signed in as another account. If the browser device-code flow completed with a different user, the Azure CLI subscription, `azd` account, and existing `azd` environment could diverge.

## User-Facing Change

`./deploy.sh` now refuses to retarget an existing `azd` environment when its stored subscription or tenant differs from the active Azure CLI context. It also re-checks `azd` after device-code login and fails if the browser completed sign-in with a different account. The mismatch message explicitly states that the stored target comes from the existing `azd` environment state, not from the repository `.env` file.

## API/IaC Diff Summary

- Added an existing `azd` environment subscription/tenant guard in `deploy.sh` before mutating environment values.
- Added `ELB_ALLOW_AZD_ENV_RETARGET=true` as an explicit escape hatch for intentional retargeting.
- Added post-device-code validation that `azd auth login --check-status` contains the active Azure CLI user.

## Validation Evidence

- `bash -n deploy.sh`
- `git diff --check -- deploy.sh docs/features_change/2026-05/2026-05-22-deploy-auth-context-guard.md`