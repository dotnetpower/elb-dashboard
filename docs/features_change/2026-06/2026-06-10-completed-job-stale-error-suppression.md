# Completed BLAST jobs no longer show a stale `worker_lost` error

## Motivation

A BLAST job whose Run details page showed **Status: completed** with 3 result
files (7.3 KB) still rendered a red `worker_lost` line at the bottom of the Job
Details card. A successfully-completed job must not look like it failed.

Root cause is a state-machine cleanup gap, not a runtime failure:

1. The job's submit worker was transiently lost (`WorkerLostError`) — the worker
   sidecar was restarted / OOM-killed while `elastic-blast submit` was mid-flight
   — or the row simply went quiet, so the `reconcile_stale_jobs` beat stamped the
   row `status=failed, error_code=worker_lost`.
2. The actual BLAST runtime in AKS kept running independently and produced
   results. A later `_reconcile_row_k8s_status` / external refresh / artifact
   finalize detected completion and flipped `status` + `phase` to `completed`.
3. **None of those completion paths clear the top-level `error_code` column.**
   `_reconcile_row_k8s_status` passes `error_code=""` only into the payload
   `_progress` block (not the indexed column); the Celery-SUCCESS and
   external-refresh branches call `repo.update(status=…, phase=…)` with no
   `error_code`; `finalize_job_artifacts` never touches it.
4. The job projection `_job_error_for_response` only suppressed `error_code` for
   `status == "running"`, so a `completed` row surfaced `error="worker_lost"`,
   and the SPA's `shouldShowNonTerminalJobError` painted it red.

## User-facing change

A successfully-completed job (`status == "completed"`) no longer shows a stale
transient error (e.g. `worker_lost`) on the Run details page. The success banner
remains the terminal representation. Genuinely failed/cancelled jobs are
unchanged — they still surface their `error`.

## API / IaC diff summary

- `api/services/blast/job_state.py` — `_job_error_for_response` now returns `""`
  when `status == "completed"` (mirrors the existing `running` +
  `blast_submit_lock_busy` suppression). The raw `error_code` field is still
  emitted for diagnostics; only the user-facing `error` is suppressed. This is
  the single read-path chokepoint, so it also fixes already-stuck terminal rows
  on the next poll (they are never re-reconciled).
- `web/src/pages/blastResultsModel.ts` — `shouldShowNonTerminalJobError` adds a
  terminal-success guard (`status === "completed" || phase === "completed"` →
  `false`) as defense-in-depth so the rule holds even if a caller hands the
  component a completed job that still carries an `error` string.
- No IaC change. No data migration (display-layer fix).

## Validation evidence

- `uv run pytest -q api/tests` → 3203 passed, 3 skipped.
- New backend test
  `test_local_to_blast_job_suppresses_stale_error_on_completed_job`.
- New frontend test "suppresses a stale error on a successfully-completed job"
  (`web/src/pages/blastResultsModel.test.ts`, 7 passed).
- `uv run ruff check` clean; `npx tsc --noEmit` clean.

## Noted follow-up (not implemented)

The stored row keeps `error_code="worker_lost"` after completion. The display is
now correct, but for storage hygiene a future change could also clear the
top-level `error_code` on the three reconcile completion writes
(`_reconcile_row_k8s_status` completed branch, the Celery-SUCCESS→completed
branch, and the external-refresh→completed branch). Deferred to keep this change
minimal and because it does not affect the user-visible outcome.
