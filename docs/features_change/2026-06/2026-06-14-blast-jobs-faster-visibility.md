---
title: BLAST jobs surface faster in Recent searches and the dashboard
description: Eager cache invalidation on submit plus an active-aware poll cadence so a new job and its status transitions appear within seconds instead of 20-60 s.
tags:
  - ui
  - blast
---

# Faster job visibility in Recent searches and the dashboard

## Motivation

After submitting a BLAST search, the new job — and its `queued → running →
completed` transitions — took noticeably long to appear in **Recent searches**
and the dashboard **jobs** surfaces. The delay was entirely client-side; the
backend already creates the job row synchronously in the submit route and
resets its jobs-list cache on create. Three compounding frontend causes:

1. **Global `staleTime: 60_000`** ([web/src/main.tsx](../../../web/src/main.tsx)) — a
   jobs list fetched in the last minute is considered fresh, so navigating back
   to Recent searches / the dashboard shows the cached, job-less list **without
   refetching**.
2. **No eager cache invalidation on submit** — `useSubmitMutation.onSuccess`
   navigated to the result page but never invalidated the `["blast-jobs"]`
   queries, so those lists only updated on their next timed poll.
3. **Static slow poll cadence** regardless of whether jobs were active: Recent
   searches 20 s, dashboard JobCard 30 s (auto-refresh default), topbar chip
   15 s, and the per-cluster active-submissions strip 60 s. A status transition
   could lag a full interval (plus up to the backend's 10 s jobs-list cache
   TTL).

## User-facing change

- Submitting a search now **immediately refreshes** every job list, so the new
  job appears the moment you land on Recent searches or the dashboard instead of
  up to ~20–30 s later.
- While any job is **queued or running**, the job lists poll every **5 s**, so
  status changes show within a few seconds. Once every job is terminal the lists
  ease back to their calm idle cadence (Recent searches 20 s, topbar 15 s,
  dashboard JobCard = the auto-refresh dropdown value, cluster strip 60 s), so
  idle dashboards keep the same low-traffic posture. Background tabs still pause
  polling (`refetchIntervalInBackground: false`), and the backend's 10 s
  stale-while-revalidate jobs-list cache absorbs the faster cadence, so there is
  no per-poll backend fan-out.

## API / IaC diff summary

None. Frontend-only — no API, schema, or infrastructure change, and no redeploy
of the backend image is required.

## Implementation

- [web/src/hooks/useScopedBlastJobs.ts](../../../web/src/hooks/useScopedBlastJobs.ts) —
  new exported `blastJobsRefetchInterval({ activeMs, idleMs })` returning a
  `refetchInterval` callback that polls `activeMs` while any listed job is
  active (via `isDashboardJobActive`) and `idleMs` otherwise. Option
  `refetchInterval` widened to accept the callback form (typed against a minimal
  `JobsListQueryLike` so it stays assignable to TanStack's generic).
- [web/src/pages/blastSubmit/useSubmitMutation.ts](../../../web/src/pages/blastSubmit/useSubmitMutation.ts) —
  `onSuccess` now calls `queryClient.invalidateQueries({ queryKey: ["blast-jobs"] })`
  before navigating.
- Consumers switched to the dynamic interval:
  [useBlastJobsState.ts](../../../web/src/pages/BlastJobs/useBlastJobsState.ts) (5 s / 20 s),
  [useLatestBlastJob.ts](../../../web/src/hooks/useLatestBlastJob.ts) (5 s / 15 s),
  [JobCard.tsx](../../../web/src/components/cards/JobCard.tsx) (5 s / auto-refresh value),
  [useClusterActiveSubmissions.ts](../../../web/src/components/ClusterItem/useClusterActiveSubmissions.ts)
  (5 s / 60 s while running, paused when stopped).

## Validation evidence

- New `web/src/hooks/blastJobsRefetchInterval.test.ts` — 7 cases (active/queued/
  mixed/terminal/empty/no-data/caller-idle), all pass.
- `npx vitest run` (full web suite) — 97 files, 870 passed.
- `npm run build` — TypeScript strict + Vite build clean (EXIT=0).
- `npx eslint` on the changed files — clean.
- Diagnosis confirmed against the backend: the submit route
  ([api/routes/blast/submit.py](../../../api/routes/blast/submit.py)) calls
  `repo.create(state)` + `_reset_jobs_list_cache()` synchronously, so the row
  exists immediately and the latency was purely the client cache + poll cadence.
