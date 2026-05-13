# Terminal Managed Identity Login

## Motivation

Remote Terminal VMs are created with a system-assigned Managed Identity and receive RBAC for storage, ACR, and workload operations, but the shell bootstrap and UI still told users to run `az login --use-device-code`. That made the terminal look dependent on an interactive personal Azure CLI session.

## User-facing change

Remote Terminal shells now use the VM Managed Identity by default. New VMs configure Azure CLI login through `elb-az-login-mi`, set azcopy to MSI authentication, and show Managed Identity guidance in the terminal MOTD and web UI. Device-code login remains available only when a user intentionally wants a personal Azure CLI session.

## API/IaC diff summary

- Terminal cloud-init now writes an `elb-az-login-mi` helper that runs `az login --identity --allow-no-subscriptions` and sets the VM subscription when discoverable from IMDS.
- `/etc/profile.d/elb-env.sh` now uses `AZCOPY_AUTO_LOGIN_TYPE=MSI` and attempts the Managed Identity login for non-root terminal sessions.
- Terminal health checks now attempt Managed Identity login for `azureuser` before reporting identity status.
- VM-side BLAST fallback scripts use `AZCOPY_AUTO_LOGIN_TYPE=MSI`.
- Remote Terminal UI, setup guidance, README, and auth docs now describe Managed Identity as the default path.

## Validation evidence

Pending final validation in this change set:

- Backend lint and focused syntax checks.
- Frontend production build.
- Current production terminal VM bootstrap helper installation and `az account show` via Managed Identity.
- Production API/web deployment if validation passes.
