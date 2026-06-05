---
title: Settings — show existing VNet peerings
description: The VNet peering settings section now lists the peerings already present on the selected cluster's AKS VNet.
tags:
  - ui
  - infra
---

# Settings — show existing VNet peerings

## Motivation

The Settings → **VNet peering (OpenAPI access)** section was action-only: it
let an operator create a peering and probe, but never showed which peerings
were *already* on the selected cluster's AKS VNet. Operators had to open the
Azure portal to confirm the current state.

## User-facing change

- A read-only **Existing peerings** card now sits above the peering form.
- On cluster selection it auto-loads and renders each peering on the cluster's
  AKS VNet: remote VNet name, resource group + short subscription, peering
  state badge (Connected / Initiated / other), remote address prefixes, and the
  four allow flags (vnet access / forwarded / gateway transit / remote
  gateways).
- A **Refresh** button re-queries on demand; the list also re-loads
  automatically after a successful peer, post-grant retry, or NSG apply.
- Degraded states are explained inline instead of failing: an RBAC denial on
  the listing call shows a hint, a BYO-subnet cluster (no auto-VNet) shows a
  "no peering needed" note, and an empty VNet shows a "no peerings yet" note.

## API / IaC diff summary

- **New route** `GET /api/settings/vnet-peering/existing`
  (`subscription_id`, `resource_group`, `cluster_name`) in
  `api/routes/settings/vnet_peering.py`. Read-only, `require_caller`-protected,
  synchronous. Folds routine Azure faults into a 200 payload (`error` /
  `skipped`); only a hard helper failure becomes a 502.
- **New helper** `list_vnet_peerings_for_cluster()` in
  `api/tasks/azure/peering.py`. Reuses `_resolve_aks_vnet_id` and reads
  peering attributes already embedded in each `VirtualNetworkPeering`
  (`remote_virtual_network.id`, `remote_address_space`) — no extra ARM
  round-trip, so a cross-subscription remote VNet never triggers a second
  (possibly RBAC-denied) `get`.
- **Frontend**: `VnetPeeringExistingResponse` / `VnetPeeringExistingItem`
  types and `settingsApi.listExistingPeerings(...)` in `web/src/api/settings.ts`;
  `ExistingPeerings` / `ExistingPeeringRow` / `PeeringFlag` components in
  `web/src/components/settings/sections/VnetPeeringSection.tsx`.
- No Bicep / RBAC change. No SAS, no public-network toggle.

## Validation evidence

- `uv run ruff check api` — clean.
- `uv run pytest -q api/tests` — 2856 passed (added route + helper tests in
  `test_settings_vnet_peering.py` and `test_azure_peering.py`; registered the
  new monkeypatch target in `test_tasks_facade_contract.py`).
- `cd web && npm run build` + `eslint` on the two touched files — clean.
</content>
</invoke>
