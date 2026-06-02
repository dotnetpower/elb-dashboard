---
title: BLAST Results — live-status-paused banner when polling fails
description: Surface a visible "live updates paused" banner on the BLAST Results page when the job-status poll fails while the displayed phase is still non-terminal, so an expired session no longer leaves the user staring at a frozen "running" badge for a job that already finished.
tags:
  - blast
  - ui
---

# BLAST Results — live-status-paused banner when polling fails

## Motivation

An OpenAPI-submitted BLAST job (`30b875c7e875`) that had actually **completed**
on the backend kept showing **running** in the UI. Root cause: the browser MSAL
access token expired, so the next status poll after the backend flipped to
`completed` returned `401`. TanStack Query keeps the last successful snapshot
mounted on error, so the Results page sat on the stale `running` snapshot
indefinitely. A global session-issue gate (`AuthenticatedApp` → `<SignIn expired>`)
exists and the poll's `401` raises `notifyAuthSessionIssue("api_unauthorized")`,
but a backgrounded tab may never follow the redirect — leaving the user misled by
a frozen status with no visible cue.

## User-facing change

The BLAST Results page now renders a **"Live status updates paused"** warning
banner directly under the job header whenever the job-status poll is failing
(`jobQuery.isError`) **and** the displayed phase is still non-terminal. The banner:

- explains the status shown below could be out of date and the job may have
  already finished;
- when the poll failed with `401`, additionally notes the browser sign-in
  session may have expired;
- offers a **Refresh now** button that re-runs the status query (spinner while
  fetching).

When the poll succeeds again — or the job is already terminal — the banner
disappears.

## API / IaC diff summary

No backend, API contract, or infra change. Frontend only:

- `web/src/pages/blastResults/useBlastResultsState.ts` — compute and expose two
  additive flags from the existing `jobQuery`: `liveUpdatesStalled`
  (`isError && job && phase non-terminal`) and `liveUpdatesStalledAuthExpired`
  (the error status is `401`). Return shape is additive (`as const`), backward
  compatible with all existing consumers.
- `web/src/pages/BlastResults.tsx` — render the banner between the job header
  and the results tabs; wired to `state.jobQuery.refetch()`.

## Validation evidence

- `cd web && npm run build` → exit 0 (built in 7.17s).
- `cd web && npm test -- --run` → 64 files / 524 tests passed.
- `git diff --stat` confirms only the two intended files changed (+59 insertions).
