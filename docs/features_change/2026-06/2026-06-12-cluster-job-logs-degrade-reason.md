---
title: Cluster Job logs surface the degrade reason instead of a blank "(empty)"
description: When a completed BLAST Job's Spot node is reclaimed the on-cluster pod log is gone; the logs dialog now explains why instead of rendering an opaque "(empty)".
tags:
  - ui
  - operate
---

# Cluster Job Logs — Surface the Degrade Reason (#28)

## Motivation

The cluster **Workloads → Jobs** tab (and the per-row "Job Logs" modal)
rendered `(empty)` for many completed BLAST search Jobs. The logs were not
actually empty: the backend hit an exception fetching the pod log and silently
degraded to an empty string, which the SPA rendered as `(empty)`. The real
cause — the Job's Azure Spot node was reclaimed after completion, so the pod
object lingers but its log is no longer readable — was lost to the user.

## User-facing change

The logs dialog now distinguishes three states:

- **Real log body** → shown as before.
- **Degraded (backend returned `degraded: true` with `logs: ""`)** → an
  explicit message. For a **Job** it reads: *"Logs are no longer available
  (reason: …). The Job has finished and its node was likely reclaimed (BLAST
  search Jobs run on Azure Spot nodes), so the on-cluster pod log is gone. The
  BLAST results were already shipped to Storage — view them from the job's
  results instead."* For Pods/Deployments a generic node-reclaim message is
  shown.
- **Genuinely empty** → still `(empty)`.

## API / IaC diff summary

No backend or schema change — the `_graceful` path in
[api/routes/monitor/common.py](../../../api/routes/monitor/common.py) already
returns `degraded` / `degraded_reason`. Frontend-only:

* [web/src/api/monitoring.ts](../../../web/src/api/monitoring.ts) — the
  `pod-logs` / `deployment-logs` / `job-logs` typed clients now expose the
  optional `degraded?` / `degraded_reason?` fields the backend already sends.
* [web/src/components/ClusterDiagnostics/useWorkloadActions.tsx](../../../web/src/components/ClusterDiagnostics/useWorkloadActions.tsx)
  — `fetchLogs` reads `degraded` and renders `degradedLogMessage(kind, reason)`
  instead of collapsing everything to `(empty)`.

## Validation evidence

* `cd web && npm run build` → success.
* `npx vitest related --run …BlastHitsTable.tsx …useWorkloadActions.tsx …monitoring.ts`
  → 87 passed.
* The fields are additive + optional, so existing callers and mocks are
  unaffected.
