---
title: Remove or hide orphaned VNet peerings
description: Detect peerings whose remote VNet was deleted, warn the operator, and let them delete the stale peering or hide it from the Settings list.
tags:
  - infra
  - ui
---

# VNet peering — orphaned ("Disconnected") peering cleanup

## Motivation

When a peered VNet is deleted, its peering on the AKS auto-VNet lingers in the
`Disconnected` state forever. The Settings panel rendered it with a muted badge
and no guidance, and there was no way to remove it or hide it — so the list
accumulated dead entries with no recourse short of the Azure Portal.

## User-facing change

- Each existing peering is now classified: `ghost` (remote VNet confirmed
  deleted), `disconnected` (Disconnected but the remote could not be confirmed
  gone), or `healthy`.
- A `ghost` / `disconnected` peering shows an amber warning explaining the
  remote VNet no longer exists (or may have been deleted) and offers two
  actions:
  - **Delete peering** — removes the stale AKS-side peering from Azure (uses the
    shared managed identity, symmetric with the create path; idempotent).
  - **Hide** — hides the row from the dashboard via `localStorage` (cosmetic,
    per-cluster, never touches Azure) for operators who cannot or do not want to
    delete it.
- Hidden rows are counted in a footnote so they are never silently lost.

## API / IaC diff summary

- New route `POST /api/settings/vnet-peering/delete` — validates
  `subscription_id` / `resource_group` / `cluster_name` / `peering_name`,
  delegates to `delete_vnet_peering_on_cluster`, returns the best-effort result
  (a 502 only on a hard helper error).
- `list_vnet_peerings_for_cluster` now enriches each `Disconnected` peering with
  a tri-state `remote_vnet_exists` (best-effort probe of the remote VNet;
  `True` / `False` / `null`). Connected/Initiated peerings are not probed.
- TS type `VnetPeeringExistingItem` gains `remote_vnet_exists: boolean | null`;
  new `deletePeering` client method + request/response types.

## Code summary

- [api/tasks/azure/peering.py](../../../api/tasks/azure/peering.py) —
  `_remote_vnet_exists`, `delete_vnet_peering_on_cluster`, list enrichment.
- [api/routes/settings/vnet_peering.py](../../../api/routes/settings/vnet_peering.py) —
  `delete_existing_peering` route.
- [web/src/api/settings.ts](../../../web/src/api/settings.ts) — types + client.
- [web/src/components/settings/sections/VnetPeeringSection.tsx](../../../web/src/components/settings/sections/VnetPeeringSection.tsx),
  [peeringHealth.ts](../../../web/src/components/settings/sections/peeringHealth.ts),
  [dismissedPeerings.ts](../../../web/src/components/settings/sections/dismissedPeerings.ts) — UI + helpers.

## Validation

- `uv run pytest -q api/tests/test_azure_peering.py api/tests/test_settings_vnet_peering.py`
  — new orphan-probe, delete-helper, and delete-route tests pass.
- `npx vitest run src/components/settings/sections` — `peeringHealth.test.ts`,
  `dismissedPeerings.test.ts` pass.
- `cd web && npm run build` clean.
