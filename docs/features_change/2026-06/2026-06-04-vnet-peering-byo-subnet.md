---
title: VNet peering works for BYO-subnet AKS clusters
description: Settings → VNet peering now resolves the AKS VNet from the agent-pool subnet when the cluster runs in BYO-subnet mode, instead of silently skipping.
tags:
  - operate
  - infra
---

# VNet peering for BYO-subnet AKS clusters

## Motivation

Settings → VNet peering returned HTTP 200 but the payload was
`{"skipped": true, "reason": "aks_node_rg_has_no_vnet"}` and the private-IP
probe timed out, so the feature silently did nothing for the live
`elb-cluster-02` cluster.

Root cause: `elb-cluster-02` runs in **BYO-subnet mode** — both agent pools
(`systempool`, `blastpool`) reference an operator-managed subnet
(`vnet-elb-dashboard/snet-aks`), and the AKS `MC_*` node resource group holds
**no VNet**. The peering helper only resolved the AKS VNet by listing VNets
inside the `MC_*` RG (`_resolve_aks_node_vnet`), which is empty in BYO mode, so
the whole flow short-circuited to a skip and never created any peering.

## User-facing change

- VNet peering now resolves the AKS VNet from the cluster's agent-pool
  `vnet_subnet_id` when the `MC_*` node RG has no VNet, so BYO-subnet clusters
  peer correctly with a target VNet.
- When the chosen target VNet *is* the AKS VNet (common in BYO mode, where the
  AKS VNet equals the dashboard platform VNet), the helper returns a clear,
  human-readable skip — `reason: "target_vnet_is_aks_vnet"` with a `message`
  explaining that VMs in that VNet already reach the OpenAPI private IP
  directly — instead of letting ARM reject a self-peering. The SPA skip banner
  now shows `message` (falling back to `reason`).
- Provision-time peering (`ensure_vnet_peering_with_cluster`) skips cleanly with
  `reason: "aks_shares_dashboard_vnet"` when the AKS VNet is the dashboard VNet
  (no self-peer attempt). This is treated as success by `provision_aks`.

## API / code diff summary

- `api/tasks/azure/peering.py`:
  - New `_vnet_id_from_subnet_id`, `_resolve_aks_vnet_id` (managed-VNet first,
    BYO-subnet fallback via `first_node_subnet_id`), and `_normalise_vnet_id`.
  - `ensure_vnet_peering_with_target` and `ensure_vnet_peering_with_cluster`
    use `_resolve_aks_vnet_id` and add self-peer guards.
- `api/tasks/azure/peering_nsg.py`: `_resolve_vnet_pair` uses
  `_resolve_aks_vnet_id` so the NSG reachability probe resolves the same VNet.
- `web/src/api/settings.ts`: `VnetPeeringResponse` gains optional `message?`.
- `web/src/components/SettingsPanel.tsx`: skip banner shows
  `result.message ?? result.reason`.

No infra/Bicep change — the fix ships with the normal `api` sidecar image
rebuild and SPA build that `scripts/dev/quick-deploy.sh` already performs.

## Validation evidence

- Live: `_resolve_aks_vnet_id(cred, subscription_id=<moonchoi>,
  node_resource_group=<elb-cluster-02 MC_ RG>, cluster=<elb-cluster-02>)`
  returns `.../rg-elb-dashboard/providers/Microsoft.Network/virtualNetworks/vnet-elb-dashboard`
  (previously returned `""`).
- `uv run pytest -q api/tests/test_azure_peering.py api/tests/test_peering_nsg.py api/tests/test_settings_vnet_peering.py` → 57 passed.
- `uv run pytest -q api/tests/test_tasks_facade_contract.py api/tests/test_azure_provision_aks.py` → 62 passed.
- `uv run ruff check api/...` → clean. `cd web && npm run build` → success.
- New tests: `test_target_helper_resolves_byo_subnet_vnet_when_node_rg_empty`,
  `test_target_helper_skips_self_peering_when_target_is_aks_vnet`,
  `test_cluster_helper_skips_when_aks_shares_dashboard_vnet`.
