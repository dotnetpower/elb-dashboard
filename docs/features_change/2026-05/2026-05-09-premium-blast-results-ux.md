# Premium BLAST Results UX

**Date**: 2026-05-09

## Motivation

The BLAST job detail page (`BlastResults.tsx`) had a basic progress stepper with
inconsistent phase ordering, no elapsed timer, and no visual feedback on
completion or failure. The orchestrator also did not set `custom_status` to
`completed` before returning, causing the frontend to show a stale phase.

## Changes

### Backend (`api/orchestrators/submit_blast.py`)

- Added `context.set_custom_status({"phase": result_phase, "job_id": job_id})`
  before the final `return`, so the orchestrator's `custom_status.phase`
  reflects `completed`/`failed` instead of being stuck on `exporting_results`.

### Frontend (`web/src/pages/BlastResults.tsx`)

1. **Phase order fixed**: Stepper now matches orchestrator sequence
   (`checking_vm → enabling_storage → uploading → …`) — previously
   `enabling_storage` and `uploading` were swapped.

2. **Per-phase icons**: Each step displays a lucide icon (`Server`, `HardDrive`,
   `Upload`, `Settings`, `Send`, `Dna`, `Package`, `Trophy`) instead of plain
   numbers. Completed steps show `CheckCircle2`, current step shows a spinning
   `Loader2`.

3. **Glow effects**: Current step has a blue box-shadow; completed steps have a
   green glow. CSS transitions (0.3 s) animate state changes.

4. **Elapsed timer**: A live-updating `ElapsedTimer` component (using
   `setInterval`) shows elapsed time in the page header next to the job title
   while the job is running.

5. **Phase priority fix**: When `runtime_status === "Completed"`, phase is
   overridden to `"completed"` regardless of `custom_status.phase`. This
   prevents a stale `exporting_results` display if the orchestrator didn't set
   the final status (older running instances).

6. **Completion banner**: Green gradient banner with Trophy icon and file count.

7. **Failure banner**: Red gradient banner with XCircle icon and truncated error.

8. **Toast notifications**: Phase transitions to `completed` or `failed` trigger
   a toast.

9. **Phase messages**: `PHASE_MESSAGES` lookup table for human-readable status
   descriptions (e.g. "Verifying Remote Terminal VM is running…").

10. **Better stepper layout**: Short uppercase labels (`VM CHECK`, `STORAGE`,
    `UPLOAD`, …, `DONE`), column layout with connector lines, consistent
    spacing.

## Validation

- BLAST job `job-3c417e30ef44` submitted and monitored end-to-end.
- All 8 phases transitioned correctly in the browser.
- Elapsed timer updated in real time.
- Completion banner and all-green stepper displayed on finish.
- 13 Python tests pass, 0 TypeScript errors.
