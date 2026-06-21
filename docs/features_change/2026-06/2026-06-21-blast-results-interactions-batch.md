---
title: BLAST Results interactions batch (per-tab scroll restore + degraded retry)
description: Result tabs remember their scroll position when switching, and the degraded job-listing notice gains a Retry button.
tags:
  - blast
  - ui
---

# BLAST Results interactions batch

## Motivation

Two more deferred items are now code-verifiable: switching result tabs always
dumped the user back to the top, and the degraded job-listing notice had no way
to retry without a full reload.

## User-facing change

- **#21 Per-tab scroll restore** — each result tab remembers its window scroll
  position; returning to a tab restores where you were instead of jumping to the
  top. The **Run details** tab is excluded so its live-log tail-follow keeps
  ownership of scroll.
- **#40 Degraded retry** — when the BLAST job listing is degraded, the notice
  now shows a **Retry** button that re-runs the listing query (spinner while in
  flight).

## Code change summary

- [web/src/pages/BlastResults.tsx](../../../web/src/pages/BlastResults.tsx):
  a `scrollByTabRef` records the active tab's scroll on every scroll event; a
  second effect restores the saved position (via `requestAnimationFrame`) on tab
  change, skipping the `run` tab.
- [web/src/pages/BlastJobs/JobsEmptyState.tsx](../../../web/src/pages/BlastJobs/JobsEmptyState.tsx):
  `NoJobsEmpty` accepts `onRetry` / `retrying` and renders a Retry button into
  the `DegradedNotice` action slot.
- [web/src/pages/BlastJobs/BlastJobs.tsx](../../../web/src/pages/BlastJobs/BlastJobs.tsx):
  passes `onRetry={() => jobsQuery.refetch()}` + `retrying={jobsQuery.isFetching}`.

## Validation evidence

- `cd web && npm run build` → clean.
- `cd web && npm test -- --run BlastJobs blastJobs` → 19 passed.
- `npx eslint` on the three changed files → clean.

No backend / API / IaC changes.
