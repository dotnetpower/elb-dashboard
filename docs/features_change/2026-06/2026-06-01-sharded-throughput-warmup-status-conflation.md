---
title: Sharded throughput gate distinguishes "checking" from "not warm"
description: Fixes the BLAST submit screen falsely disabling sharded performance modes for an already-warm database while warm status is still loading, plus cluster-card clarity tweaks.
tags:
  - blast
  - ui
  - aks
---

# Sharded throughput gate distinguishes "checking" from "not warm"

## Motivation

On a deployed cluster (`elb-cluster-02`) where `core_nt` warmup had completed,
the New Search screen showed **Sharded throughput** as disabled with the message
"Warm this database on the selected cluster before using sharded performance
modes." The database was in fact warm.

Root cause was a **client-side conflation**, not a backend problem. The deployed
`/api/monitor/aks/warmup-status` endpoint returned `warm: true` for `core_nt`
(`status: "Ready"`, `sources: ["warmup"]`, `progress_pct: 100`), the storage
metadata reported `sharded=true` with a valid `web_blast_searchsp`, and the
computed capacity plan was feasible (picked 10 shards, node RAM headroom OK). The
frontend hook derived `isDbAlreadyWarm = false` whenever `warmupQuery.data` was
`undefined` — i.e. while the query was loading, disabled (cluster not yet
`isAksWorkloadReady`), or errored — and the sharding gate treated that unknown
state identically to a confirmed not-warm state. A freshly-completed warmup also
required a manual page reload to be reflected.

## User-facing change

- The sharding gate now distinguishes **unknown/checking** from **confirmed not
  warm**. While warm status is still loading it shows a neutral
  "Checking warm status on the selected cluster…" message instead of falsely
  telling the user to warm an already-warm database. (Sharded modes stay disabled
  in both states — that is the safe default — but the copy is now accurate.)
- A bounded `refetchInterval` (20 s) on the warmup-status query means a freshly
  completed warmup is picked up automatically within ~20 s without a manual
  reload; polling stops once the selected DB is confirmed warm.
- Cluster card clarity tweaks (advisory UX findings):
  - Subtitle replaces "anchor:" jargon with
    "Workspace RG: … · clusters listed subscription-wide".
  - The latency KPI label reads "Control-plane API p95" instead of
    "Dashboard p95".
  - While a new cluster is provisioning, the in-flight cluster is no longer shown
    twice (the duplicate list row is filtered; the KPI count is unchanged).

## API / IaC diff summary

Frontend only — no backend, Celery, or infra change.

- `web/src/pages/blastSubmit/useWarmupStatus.ts` — moved `selectedDbShortName`
  ahead of the query, added a bounded `refetchInterval`, and exposed
  `isWarmupStatusResolved` (`warmupQuery.data !== undefined`).
- `web/src/pages/blastSubmit/shardingAvailability.ts` — added optional
  `isWarmupStatusResolved` (defaults `true` for backward compatibility) and a
  neutral "Checking warm status…" reason.
- `web/src/pages/BlastSubmit.tsx` — threads the new flag into
  `deriveShardingAvailability`.
- `web/src/components/cards/ClusterCard/ClusterCard.tsx` — subtitle / p95 label
  copy and `visibleClusters` dedup during provisioning.

## Validation evidence

- Deployed backend `/api/monitor/aks/warmup-status` (real bearer token, audience
  `api://14cf2a04-…`) returned HTTP 200 `warm: true` for `core_nt` — confirming
  the disabled state was purely client-side.
- `cd web && npm test -- --run` — 463 passed (includes new
  `shardingAvailability.test.ts` "checking" case).
- `cd web && npm run build` — clean.
- Local-debug storage surface re-closed afterwards: `publicNetworkAccess=Disabled,
  defaultAction=Deny, ipRules=[]`.
