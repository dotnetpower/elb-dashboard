---
title: api sidecar bumped to 1.0 vCPU / 2.0Gi
description: Raise the api sidecar from 0.5 vCPU / 1.0Gi to 1.0 vCPU / 2.0Gi to relieve CPU-saturation bursts and a ~80% memory baseline.
tags:
  - infra
  - operate
---

# api sidecar bumped to 1.0 vCPU / 2.0Gi

## Motivation

Live diagnosis of the deployed Container App showed the `api` sidecar hitting
**99.2% of its 0.5 vCPU** (Sizing card normalizes the cgroup CPU% to the
sidecar allocation) and sitting at **79.7% of its 1.0Gi** working set (a
`Watch` severity, threshold ≥70%). Log Analytics over the prior 4h attributed
the CPU bursts to slow synchronous hot paths overlapping on the single
half-core — notably `/api/blast/jobs/{id}` (max 27.5s), `/api/monitor/acr`
(13.3s), and high-frequency `/api/monitor/message-flow` polling. No OOMKill
had occurred yet (RestartCount = 0), but the half-core / 1Gi envelope was too
tight for the api sidecar's public-ingress + reverse-proxy + monitor fan-out +
JWT-validation workload.

## User-facing change

- The api sidecar now has **1.0 vCPU / 2.0Gi** (was 0.5 vCPU / 1.0Gi).
- On the Settings → Sizing card, the api row CPU/Memory meters are now
  relative to the larger allocation, so the same absolute load reads roughly
  half the previous utilization percentage (e.g. a burst that pegged 99% of
  0.5 vCPU now reads ~50% of 1.0 vCPU).
- Container Apps Consumption enforces a fixed `1 vCPU : 2 GiB` ratio, so
  `1.5 vCPU / 2Gi` was not a valid pair; the chosen `1.0 / 2.0` keeps the
  ratio and the per-replica aggregate well under the `4 vCPU / 8 Gi` cap.

## API / IaC diff summary

- [infra/modules/containerAppControl.bicep](../../../infra/modules/containerAppControl.bicep):
  api `resources` `cpu: json('0.5') → json('1.0')`, `memory: '1.0Gi' → '2.0Gi'`,
  plus a rationale comment; the worker-block aggregate comment updated to
  `3.25 vCPU / 6.5Gi (api 1.0/2.0, …)`.
- `infra/main.json` + `infra/modules/containerAppControl.json`: recompiled from
  the Bicep (inlined module + templateHash refresh).
- [web/src/components/settings/sections/SizingSection.tsx](../../../web/src/components/settings/sections/SizingSection.tsx):
  `SIDECAR_RESOURCES.api` `{cpu:0.5, memoryGi:1.0} → {cpu:1.0, memoryGi:2.0}`
  so the Sizing card normalizes against the live allocation.

Per-replica aggregate: `2.75 vCPU / 5.5Gi → 3.25 vCPU / 6.5Gi` (under the
Consumption `4 vCPU / 8 Gi` cap).

## Validation evidence

- `az containerapp update --container-name api --cpu 1.0 --memory 2.0Gi` →
  new revision `ca-elb-dashboard--0000139`, `provisioningState: Succeeded`.
- `az containerapp show … containers[]` confirms api `1.0 / 2Gi` with the other
  five sidecars unchanged (frontend 0.25/0.5, worker 1.0/2, beat 0.25/0.5,
  redis 0.25/0.5, terminal 0.5/1).
- `curl …/api/health` → `http_status=200` (52ms) through public ingress.
- `cd web && npm run build` → built clean in 3.24s.

## Follow-ups (not in this change)

- `SIDECAR_RESOURCES.worker` in `SizingSection.tsx` is stale (`0.5/1.0`) while
  the live worker is `1.0/2.0Gi`; its Sizing row under-reports the allocation.
  Separate one-line fix, unrelated to the api bump.
- Code-level: profile why `/api/blast/jobs/{id}` can take ~27s (synchronous
  result analysis on the hot path) to reduce the CPU bursts at the source.
