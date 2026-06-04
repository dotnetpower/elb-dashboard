---
title: BLAST job list distinguishes Queued from Running
description: Jobs waiting for a submit slot or capacity now render as "Queued" instead of being mislabelled "Running".
tags:
  - user-guide
  - blast
---

# BLAST job list distinguishes Queued from Running

## Motivation

In the BLAST execution list (and the dashboard ClusterBento roster), every active
job rendered as **RUNNING**, even when it was actually waiting in line. A job in
the `waiting_for_submit_slot` / `waiting_for_capacity` phase showed the orange
RUNNING badge while the cluster header chip correctly said `WAITING_FOR_SUBMIT_SLOT`,
contradicting each other.

### Root cause

The backend `submit_task` deliberately persists `status="running"` for the
`waiting_for_submit_slot`, `waiting_for_capacity`, and `capacity_reserve_lost`
phases. This is a **reconciler keep-alive sentinel** — it keeps the job row
"active" so the reconciler keeps polling it; the true sub-state lives in `phase`.

The frontend `classifyJobState` prioritised `status` over `phase`
(`if (statusState) return statusState;`) and did not recognise the waiting phases,
so the `status="running"` sentinel always won and the row was classified as
`Running`.

## User-facing change

- New display state **Queued** (calm grey tone, `--text-muted`) added between
  Pending and Running.
- Jobs whose phase is `queued`, `waiting_for_submit_slot`, `waiting_for_capacity`,
  or `capacity_reserve_lost` now render as **QUEUED** in the BLAST job list, the
  ClusterBento roster, and the ClusterPulse preview — even though the backend
  still carries the `status="running"` reconciler sentinel.
- A terminal outcome still wins: a job that reaches `failed`/`completed` while its
  phase is a waiting phase is shown as Failed/Completed (queued precedence only
  applies over the running sentinel).
- Queued jobs remain **active** (elapsed-timer tick and the per-day "N active"
  label still include them), but the canonical follow-up below splits them out of
  the **running** count and filter and gives them their own count/tab/timer label.
- The **Job Details** page (`/blast/jobs/:id`) Status field, the completed-job
  metric strip, and the in-progress "Current phase" hint now render the
  queued-family phases (`waiting_for_submit_slot`, `waiting_for_capacity`,
  `capacity_reserve_lost`, `queued`) as **queued** instead of the internal phase
  id, matching the job list badge.

## Code diff summary

- `web/src/components/cards/ClusterBento/jobTypes.ts` — added `"Queued"` to the
  `DisplayJobState` union.
- `web/src/components/cards/ClusterBento/jobMapping.ts` — new `QUEUED` phase set;
  `classifyValue` and `classifyJobState` map the waiting phases to `Queued`,
  honouring queued phase over the `status="running"` sentinel; `isActiveJobState`
  includes `Queued`.
- `web/src/components/cards/ClusterBento/atoms.tsx` — `JOB_STATE_TONES.Queued`.
- `web/src/components/cards/ClusterPulse/helpers.ts` — `jobStateTone` /
  `jobTimeText` handle `Queued`.
- `web/src/components/cards/ClusterPulse/usePulseSignals.ts` — `JOB_STATE_ORDER`
  sorts `Queued` first.
- `web/src/constants.ts` — `STATUS_COLORS` entries for queued phases, plus a new
  `phaseLabel(phase)` helper that maps the queued-family phases to "queued".
- `web/src/pages/blastResults/BlastJobDetailsGrid.tsx`,
  `web/src/pages/blastResults/BlastJobMetrics.tsx`,
  `web/src/pages/blastResults/ResultsBody.tsx` — Job Details Status / metric strip /
  in-progress phase hint use `phaseLabel` so they read "queued".

No backend / IaC change — the backend sentinel contract is intentionally preserved.

## Canonical follow-up — counts, filter, timer, queue reason

After the initial fix the queued jobs were correct but still lumped under the
"running" count/filter and showed an "Elapsed" timer. The canonical pass makes
the queued state first-class:

- **Single source of truth for queued phases.** `web/src/constants.ts` now exports
  `QUEUED_PHASES` (the one set of raw backend phases that mean queued). Both
  `phaseLabel` / `queueReasonText` (Job Details vocabulary) and
  `jobMapping.ts`'s `QUEUED` classifier set derive from it, so a new waiting
  phase is added in one place.
- **Counts split.** The BLAST jobs header now reads
  `N total · Q queued · R running · C completed · F failed` and the progress bar
  gained a grey queued segment. `useBlastJobsState` counts queued
  (`isDashboardJobQueued`) separately from running (`isDashboardJobRunning` =
  active **and not** queued).
- **Filter split.** A new **Queued** filter tab sits between All and Running; the
  **Running** tab now excludes queued jobs (`FilterKind` gained `"queued"`).
- **Timer label.** A queued row shows **"Queued for MM:SS"** (time since
  `created_at`) instead of "Elapsed", since it has not started on the cluster.
- **Queue reason subtext.** Both the job list row and the Job Details Status line
  render a calm secondary line explaining *why* it is waiting —
  `Waiting for submit slot` / `Waiting for cluster capacity` / `Waiting in queue`
  — driven by the single `queueReasonText(phase)` helper.
- Queued jobs remain **active** for the per-day section "N active" label and the
  live timer tick (so "Queued for" keeps counting) — `isActiveJobState` still
  includes Queued; only the counts/filter/timer/label distinguish it.

### Additional code diff

- `web/src/constants.ts` — `QUEUED_PHASES` exported; new `queueReasonText(phase)`.
- `web/src/components/cards/ClusterBento/jobMapping.ts` — `QUEUED` derived from
  `QUEUED_PHASES`; new `isQueuedJobState` / `isRunningJobState` /
  `isDashboardJobQueued` / `isDashboardJobRunning`.
- `web/src/pages/BlastJobs/useBlastJobsState.ts` — `counts.queued`; `FilterKind`
  gains `"queued"`; running filter/count exclude queued.
- `web/src/pages/BlastJobs/JobsFilterBar.tsx`, `JobsHeader.tsx` — queued tab,
  queued count, queued progress segment.
- `web/src/pages/BlastJobs/JobRow.tsx` — "Queued for" timer label + queue reason
  subtext under the badge.
- `web/src/pages/blastResults/BlastJobDetailsGrid.tsx` — queue reason line under
  the Status label.

## Validation

- `cd web && npx vitest run src/components/cards/ClusterBento/jobMapping.test.ts`
  — 14 passed, including new "queued vs running classifiers" cases.
- `cd web && npx vitest run src/constants.test.ts` — 8 cases for `phaseLabel` /
  `queueReasonText`.
- `cd web && npm test -- --run` — 68 files / 570 tests passed.
- `cd web && npm run build` — `tsc -b && vite build` clean.
