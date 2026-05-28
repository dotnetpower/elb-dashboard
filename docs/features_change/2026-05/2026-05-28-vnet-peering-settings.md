---
title: Settings — VNet peering panel
description: Surface VNet peering + private-IP probe in the Settings panel so other Azure VMs can reach the OpenAPI control-plane private IP via discovered subscription / resource group / VNet.
tags:
  - user-guide
  - operate
  - infra
---

# 2026-05-28 — Settings: VNet peering panel

## Motivation

The OpenAPI ingress for the control plane only listens on a private IP
inside the platform VNet (`10.224.0.7` in the default AKS auto-VNet
`10.224.0.0/12` range). Researchers running an analysis VM in another
VNet inside the same Azure subscription previously had no in-product
path to reach it — they had to drop into the Azure portal, pick the
right resource group / VNet, and create a bidirectional peering by
hand.

The peering helper itself already existed
(`ensure_vnet_peering_with_target` in
[api/tasks/azure/peering.py](../../../api/tasks/azure/peering.py)
and the `POST /api/settings/vnet-peering` route in
[api/routes/settings/vnet_peering.py](../../../api/routes/settings/vnet_peering.py)),
but it was never wired into the SPA. This change surfaces it so the
whole flow — pick AKS, pick target VNet, peer, probe `/openapi.json`
on the private IP — happens inside the dashboard.

## User-facing change

A new **"VNet peering"** section appears in the Settings panel between
**Public HTTPS** and **Sizing**:

- **AKS cluster (source side)** dropdowns: subscription → resource
  group → cluster. The helper auto-resolves the AKS auto-VNet from
  the cluster's `node_resource_group`, so the user does not pick the
  source VNet manually.
- **Target side** dropdowns: subscription → resource group → VNet.
  Defaults to the AKS subscription / RG so the common "VM in the same
  RG" case is one click.
- **Target IP** (default `10.224.0.7`) and **Target path** (default
  `/openapi.json`) are editable text inputs.
- **Peer & probe** button POSTs to `/api/settings/vnet-peering`,
  renders the probe status as a Badge, lists both peering legs
  (`aks_to_target`, `target_to_aks`) with their `state`, and shows the
  `recovery_command` snippet for retry / RBAC troubleshooting.

There is no behaviour change for users who do not open the new
section. The route already required `require_caller`, so the auth
contract is unchanged.

## API / IaC diff summary

- **No new backend route.** The SPA discovers the target VNet through the
  existing direct-ARM pattern in
  [web/src/api/arm.ts](../../../web/src/api/arm.ts) (the same MSAL bearer
  flow already used by `listSubscriptions` / `listResourceGroups` /
  `listStorageAccounts` / `listAcrs`), which keeps target visibility
  consistent with what the user can see in the Azure portal.
- **No new task / Celery work**: the peering helper and probe were
  already implemented and tested; this change just exposes them in
  the UI.
- **Frontend**: new typed clients `listVnets` in
  [web/src/api/arm.ts](../../../web/src/api/arm.ts) and
  `settingsApi.peerVnet` in
  [web/src/api/settings.ts](../../../web/src/api/settings.ts); new
  `VnetPeeringSection` rendered from
  [web/src/components/SettingsPanel.tsx](../../../web/src/components/SettingsPanel.tsx).
- **Infra**: no Bicep change. The peering itself is created at
  runtime by the shared user-assigned MI
  (`id-elb-dashboard-*`), which already has Network Contributor on
  the AKS auto-VNet RG; the target RG needs the same role for the
  call to succeed (the section surfaces the `recovery_command`
  string if it doesn't).

## Validation evidence

- `uv run pytest -q api/tests` — all tests pass. Backend coverage of
  the `POST /api/settings/vnet-peering` route lives in
  [api/tests/test_settings_vnet_peering.py](../../../api/tests/test_settings_vnet_peering.py),
  and the `_FACADE_CONTRACT` guard in
  [api/tests/test_tasks_facade_contract.py](../../../api/tests/test_tasks_facade_contract.py)
  was extended for the two new monkeypatch targets
  (`ensure_vnet_peering_with_target`, `httpx.get`).
- `uv run ruff check api` — all checks passed.
- `cd web && npm run build` — built in 10.42 s, no TypeScript errors.

### curl smoke (with `AUTH_DEV_BYPASS=true` on a host-mode dev loop)

```bash
# trigger peering + private-IP probe
curl -sX POST http://127.0.0.1:8085/api/settings/vnet-peering \
  -H 'content-type: application/json' \
  -d '{
        "subscription_id": "'"$SUB"'",
        "resource_group": "rg-elb-dashboard",
        "cluster_name": "aks-elb-dashboard",
        "target_subscription_id": "'"$SUB"'",
        "target_resource_group": "rg-target",
        "target_vnet_name": "vnet-analyst-01",
        "target_ip": "10.224.0.7",
        "target_path": "/openapi.json"
      }' | jq '{probe, peerings: [.peerings[].state]}'
```
