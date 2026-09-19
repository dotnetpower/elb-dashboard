---
title: Monitoring UI (Agent Detail)
description: Card-by-card specification of the ElasticBLAST Control Plane Dashboard — readiness signals, degraded-state semantics, and the api/routes/monitor backend contract.
tags:
  - agent
  - ui
---

# Monitoring UI (detail)

> Re-verified 2026-09-16 against `DashboardGrid`, `ClusterBento`, and the
> monitor/message-flow API clients.

The dashboard is the landing page; the Browser Terminal is one tab among many.

Current composition:

1. **Cluster plane** — one full-width Cluster card. Its bento cells cover
  lifecycle/readiness, live activity, resource pulse, topology, recent runtime,
  and the read-only Capacity Gate decision preview. Job details live on the
  dedicated BLAST Jobs/Results routes rather than in a standalone dashboard
  card.
2. **Message Flow** — an optional compact strip between Cluster and Resource
  planes. It renders only while the Service Bus integration is effective-enabled
  and opens the full producer → request queue → worker → completion flow.
3. **Resource plane** — ACR, Storage, and preview-gated Browser Terminal cards.
  Storage reports the private-network posture and database readiness; ACR
  compares repository tags with `IMAGE_TAGS`; Terminal probes the loopback
  sidecar path.
4. **Sidecar runtime** — one card for all six containers in
  `ca-elb-dashboard`, including CPU, memory, restart count, health, and recent
  HTTP activity. It is hidden on narrow mobile layouts.

The global refresh control offers Live, Slow, and Manual modes. Live uses SSE
for sidecar and job invalidation with polling fallbacks; the optional Message
Flow strip independently polls every 4-5 seconds while active or idle. Do not
describe every dashboard surface as a fixed 30-second poll.

All numbers must come from real Azure / Kubernetes APIs. Never fabricate or
cache stale data without showing a last-refreshed timestamp or a clear degraded
state.
