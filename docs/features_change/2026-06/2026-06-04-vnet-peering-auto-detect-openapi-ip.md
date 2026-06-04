---
title: VNet peering auto-detects the elb-openapi private IP
description: Settings → VNet peering now resolves the OpenAPI internal-LB IP from the selected cluster instead of defaulting to a hardcoded 10.224.0.7.
tags:
  - ui
  - setup
---

# VNet peering auto-detects the elb-openapi private IP

## Motivation

The Settings → VNet peering "Peer & probe" form hardcoded the OpenAPI private
IP default to `10.224.0.7`. That address only belongs to an **auto-VNet** AKS
cluster (`10.224.0.0/16`). For a **BYO-subnet** cluster — which is the
dashboard's own topology (`elb-cluster-02` lands inside the platform VNet
`10.20.0.0/20`, elb-openapi at `10.20.4.15`) — the default was always wrong, so
the probe timed out and the operator had to look up and type the real IP by
hand every time. A stopped cluster's stale IP (`10.224.0.7` from the
auto-VNet of a Stopped `elb-cluster-01`) made this especially confusing.

## User-facing change

* The "OpenAPI private IP" field now **auto-detects** the live `elb-openapi`
  internal-LoadBalancer IP for the selected AKS cluster (via
  `GET /api/monitor/aks/service-ip`) whenever the cluster selection changes.
* The field starts empty and is filled in once detection completes; the hint
  shows the detection state ("Detecting…", "Auto-detected from `<cluster>`", or
  a manual-entry fallback when elb-openapi has no LB IP yet).
* Manual edits are respected — typing an override stops any later
  auto-detection from clobbering the value.
* The stale "default `10.224.0.7`" copy was removed from the section help text
  and the `VnetPeeringRequest.target_ip` doc comment.

## API / IaC diff summary

No API or IaC changes. The backend route
[`/api/settings/vnet-peering`](../../../api/routes/settings/vnet_peering.py) is
unchanged and still accepts an optional `target_ip`. The UI now always supplies
the resolved IP, so the backend's legacy `10.224.0.7` fallback is no longer
reached on the UI path (it remains only for direct OpenAPI callers that omit
the field, and is correct only for auto-VNet clusters — documented in
`web/src/api/settings.ts`).

Frontend changes only:

* `web/src/components/SettingsPanel.tsx` — `VnetPeeringSection`: empty initial
  `targetIp`, a cluster-scoped auto-resolve `useEffect`, touched-tracking, and
  updated hint/placeholder/help copy.
* `web/src/api/settings.ts` — updated `target_ip` doc comment.

## Validation evidence

* `cd web && npm run build` → built in ~7.8s, exit 0.
* `npx eslint src/components/SettingsPanel.tsx src/api/settings.ts` → 0 errors
  (1 pre-existing unrelated `exhaustive-deps` warning at L2139).
* Live operational state (production dashboard, prior to this code change)
  confirmed the root cause and target value: probe
  `http://10.20.4.15/openapi.json` → 200 OK · 18.6 ms, both peering directions
  Connected. This code change removes the manual-typing step that produced that
  fix.
