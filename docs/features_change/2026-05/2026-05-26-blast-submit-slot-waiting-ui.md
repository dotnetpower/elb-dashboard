# BLAST Submit Slot Waiting UI

## Motivation
When a BLAST submit was already running for the same cluster, a second submit was correctly re-queued as `waiting_for_submit_slot`. The detail page still rendered `blast_submit_lock_busy` in the generic red error panel because the dashboard projection exposed the diagnostic `error_code` as a user-facing `error` even while the job was still running.

## User-facing change
Queued BLAST submits now read as an expected waiting state instead of an error. The running banner says the job is queued behind another BLAST submit, the Submit step remains active in the timeline, and the generic red error panel is suppressed for the expected submit-lock contention code.

## API / IaC diff summary
- `api/services/blast/job_state.py` keeps `error_code=blast_submit_lock_busy` for diagnostics but no longer projects it into the user-facing `error` field while the job is running.
- `web/src/components/BlastStepTimeline/constants.ts` maps `waiting_for_submit_slot` to the Submit step and adds a friendly running message.
- `web/src/pages/blastResultsModel.ts` adds a frontend guard so older API responses with `error=blast_submit_lock_busy` are still rendered as queued waits.
- No IaC changes.

## Validation evidence
- `uv run pytest -q api/tests/test_local_to_blast_job.py`
- `cd web && npm run test -- src/pages/blastResultsModel.test.ts src/components/BlastStepTimeline/stepState.test.ts`
- `cd web && npm run build`
