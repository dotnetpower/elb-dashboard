---
title: VNet peering surfaces precise RBAC remediation on authorization failure
description: When a target-VNet peering is denied by Azure RBAC, the Settings panel now shows the exact least-privilege role grant instead of the misleading platform-to-AKS recovery command.
tags:
  - operate
  - auth
---

# VNet peering RBAC remediation

## Motivation

The Settings → VNet peering flow ("Peer & probe") failed repeatedly with
`AuthorizationFailed` / `LinkedAuthorizationFailed` and only offered the generic
`peer-cluster-network.sh` recovery command. That script calls
`POST /api/aks/peer-with-platform`, which peers **platform ↔ AKS** under the same
managed identity — it cannot fix a **target-VNet ↔ AKS** peering, so pasting it
left the operator stuck in the same error.

VNet peering requires `…/virtualNetworkPeerings/write` on **both** ends. The
dashboard managed identity holds Contributor on the AKS-VNet resource group but
has no write on the operator-selected target VNet, so both directions are denied.

## User-facing change

When a target-VNet peering fails with an Azure RBAC denial, the peering result
payload now carries an `rbac_remediation` block and the Settings panel renders it
as an error line above the (still-shown) generic recovery hint. It contains:

- a one-line explanation that peering needs write on both ends and that
  `peer-cluster-network.sh` does **not** fix target-to-AKS peering;
- a ready-to-paste, least-privilege `az role assignment create` granting
  `Network Contributor` **scoped to the target VNet** (not its resource group or
  the subscription), with the managed-identity object id parsed out of the Azure
  error message.

After running the grant and re-clicking "Peer & probe", peering succeeds.

## API / IaC diff summary

- `api/tasks/azure/peering.py`: added `_is_authorization_failure`,
  `_mi_object_id_from_error`, `_rbac_remediation`; `ensure_vnet_peering_with_target`
  now attaches `rbac_remediation` to its payload when the peering error is an RBAC
  denial. No new Azure write is performed — the helper only renders the command.
- `web/src/api/settings.ts`: `VnetPeeringResponse` gains the optional
  `rbac_remediation` object.
- `web/src/components/SettingsPanel.tsx`: renders `rbac_remediation.message` +
  `command` when present, plus two affordances — a **Copy command** button and an
  **"I granted the role — retry"** button. The retry runs `peerVnet` on a backoff
  loop (10s, then 20s, then 30s ×4 ≈ 2.5 min) to absorb Azure RBAC propagation
  delay (1-5 min) and stops as soon as the response no longer carries
  `rbac_remediation` (both peering directions succeeded). The manual "Peer &
  probe" button is disabled while a retry loop is in flight to avoid a concurrent
  round-trip.
- No IaC change. No new role is granted by the dashboard; the operator runs the
  grant out-of-band, keeping the managed identity least-privilege. The retry loop
  only re-issues the existing peering call — it never grants a role itself.

## Validation

- `uv run pytest -q api/tests/test_azure_peering.py` — 18 passed, including the new
  `test_target_helper_surfaces_rbac_remediation_on_authz_failure` (asserts role,
  target-VNet scope, parsed MI object id) and
  `test_target_helper_omits_rbac_remediation_on_non_authz_error` (no remediation on
  a non-RBAC fault).
- `uv run ruff check api/tasks/azure/peering.py api/tests/test_azure_peering.py` — clean.
- `cd web && npm run build` — succeeds.
