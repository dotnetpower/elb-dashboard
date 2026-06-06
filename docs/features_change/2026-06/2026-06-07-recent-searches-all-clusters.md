---
title: Recent searches lists jobs across all clusters
description: Stop pinning the Recent searches history and topbar latest-job chip to a single auto-discovered cluster, which hid the user's recent jobs when every cluster was Stopped.
tags:
  - blast
  - user-guide
---

# Recent searches lists jobs across all clusters

## Motivation

Live session-browser testing of the Recent searches page surfaced a silent
data-visibility bug. The page header read **"6 total"** while the backend
actually held **23** jobs for the caller. The 6 shown were stale jobs from
2026-05-27/28 on `elb-cluster-01`; the user's real recent work (17+ jobs from
2026-06-03..05) ran on `elb-cluster-02` and was completely missing from the
list. The topbar "latest job" chip had the same blind spot — it surfaced a
stale `elb-cluster-01` job as the most recent.

## Root cause

`useScopedBlastJobs` always pins the jobs query to one cluster:

1. `pickPreferredCluster` prefers a workload-ready (Running) cluster, but when
   **every cluster is Stopped** (the common idle state) none qualifies, so the
   fallback chain drops to `clusters[0]` — the alphabetically-first cluster.
2. The jobs query is then scoped to that single cluster. BLAST job rows are
   keyed by `cluster_name`, so jobs on every other cluster vanish from the
   list.

That single-cluster scope is correct for the per-cluster Dashboard tiles
(`JobCard`, `ClusterItem` active-submissions), but wrong for the two
**history / latest** views — Recent searches and the topbar chip — which must
show the caller's jobs across the whole subscription. The backend already
treats a `subscription_id`-only query (no `cluster_name`, no `resource_group`)
as "all of this caller's jobs across every cluster", so the fix is purely on
the frontend scoping side.

## User-facing change

- **Recent searches** now lists the caller's BLAST jobs across every cluster in
  the subscription. Each row keeps its cluster badge so the origin is still
  visible. An explicit `?cluster=<name>` deep-link still scopes to one cluster
  (unchanged).
- **Topbar latest-job chip** now reflects the most recent job across all
  clusters instead of the auto-pinned (often Stopped, often stale) one.
- Per-cluster Dashboard tiles are unchanged — they still scope to their own
  cluster.

## API / IaC diff summary

- `web/src/hooks/useScopedBlastJobs.ts`: new `autoSelectCluster` option
  (default `true`, preserving existing behaviour). When `false` and no explicit
  `clusterName` is given, the hook skips fleet discovery / cluster pinning and
  issues a subscription-only jobs query (no `resource_group`, no
  `cluster_name`).
- `web/src/pages/BlastJobs/useBlastJobsState.ts` and
  `web/src/hooks/useLatestBlastJob.ts`: pass `autoSelectCluster: false`.
- No backend or IaC change — the `/api/blast/jobs` subscription-only scope
  already existed.

## Validation evidence

- Live contrast that proved the bug (deployed api, authenticated session):
  `GET /api/blast/jobs?subscription_id=…` → 21 jobs;
  `…&cluster_name=elb-cluster-01` → 6 (stale 05-27/28);
  `…&cluster_name=elb-cluster-02` → 23 (recent 06-03..05). With both clusters
  Stopped, the old code pinned to `elb-cluster-01` and showed only the 6 stale
  rows.
- `npx tsc --noEmit` → clean.
- `npx vitest run` → 77 files, 717 tests passed.
- `npx eslint` on the three changed files → clean.
