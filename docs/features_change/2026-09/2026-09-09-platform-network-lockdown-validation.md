---
title: Platform network-lockdown validation
description: Validate Storage, ACR, and Key Vault independently and close the production Key Vault public path without changing application behavior.
tags: [security, infra, operate]
---

# Platform network-lockdown validation

## Motivation

The production [Azure Storage](https://learn.microsoft.com/azure/storage/common/storage-introduction)
and [Azure Container Registry](https://learn.microsoft.com/azure/container-registry/container-registry-intro)
were private-only, but [Azure Key Vault](https://learn.microsoft.com/azure/key-vault/general/overview)
still had public network access enabled despite having private endpoints. The
postprovision check inspected Storage only and inferred that Storage, ACR, and
Key Vault shared the same posture, so this split-state drift was not reported.

## Change

- `postprovision.sh` now requires the deployed Key Vault output and reads the
  public-network state of Storage, ACR, and Key Vault independently.
- With the existing `LOCKDOWN_PRIVATE_NETWORKING=true` contract, any public or
  unreadable platform resource fails deployment validation.
- Bootstrap mode remains supported and reports the exact resources that are
  still public.
- The temporary ACR build opening is restored before the final three-resource
  posture check.
- The production azd environment now persists
  `LOCKDOWN_PRIVATE_NETWORKING=true`; the existing Key Vault public path was
  closed after proving private DNS and managed-identity access from the API
  sidecar.

No role assignment, identity, API, or user-facing workflow changed.

## Hardening discipline

- Scope: network.
- No RBAC assignment was added, narrowed, or removed.
- No new runtime guard or SSE authentication path was introduced; the existing
  deployment lockdown flag is now validated accurately.
- Storage remains `publicNetworkAccess=Disabled`, `defaultAction=Deny`, with no
  IP rules. ACR and Key Vault also report public network access disabled.

## Validation

- `bash -n scripts/dev/postprovision.sh` - passed.
- `shellcheck -x scripts/dev/postprovision.sh` - passed.
- `uv run pytest -q api/tests/test_control_plane_env.py -k postprovision` - 5 passed.
- Before lockdown, the API sidecar resolved the Key Vault hostname to a private
  address and listed secret metadata through its managed identity.
- After lockdown, the same sidecar probe passed and `/api/health` returned 200.
