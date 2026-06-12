# External (`/v1/jobs`) jobs now show step content and a real error

## Motivation

Jobs that originate from the sibling OpenAPI plane (`/v1/jobs` — direct
external-API submits or Service-Bus-bridged ones) showed an empty Execution
Steps section (no Prepare Run / Configure / etc. content), and a failed
external job with no error body rendered "No detailed error was recorded by
the orchestrator." The dashboard's 8-step timeline is dashboard-native and was
only ever populated by the local Celery submit task, so the external projection
(`_external_to_blast_job`) and the synced Table row both lacked a `steps`
structure.

A subtler correctness bug: because the frontend renders all 8 steps from the
job `phase` alone, a *completed* external job previously showed **Warmup Check
✓** and **Stage DB ✓** as done — steps the sibling never runs or reports. That
is fake-success.

## User-facing change

- External `/v1/jobs` jobs now render an honest Execution Steps timeline:
  - `Prepare Run` / `Configure` / `Submit Job` / `BLAST Run` / `Export` /
    `Complete` states are derived from the real external lifecycle
    (queued → running → success/failed/cancelled).
  - `Warmup Check` and `Stage DB` are shown **skipped** (reason
    `not_reported_by_external_api`) instead of a fake "done", because the
    sibling does not run or report those node-local steps.
  - On failure the real error is attached to the inferred failed step
    (`BLAST Run` when shard activity is visible, else `Submit Job`).
  - Real sibling-reported execution detail (BLAST+ version, DB version, shard
    counts, hit count) is surfaced as the run-step output when present.
- A failed external job with no error body now shows an honest fallback
  message instead of "No detailed error was recorded".
- The same projection is applied across all three read paths so the timeline
  is consistent whether the job is fetched fresh, served from a synced Table
  row, or read via the terminal execution-steps snapshot.

## API / IaC diff summary

- `api/services/blast/external_job_projection.py` — new pure helpers
  `_external_execution_detail_text`, `_external_execution_steps`, and the
  shared `_external_step_projection`; `_external_to_blast_job` now emits
  `output.steps` / `custom_status.steps` / `output.error` /
  `output.failed_step` and an honest no-detail fallback error.
  `_clamp_error_message` now runs the message through `sanitise()` so the error
  shown in the banner and the synthesized failed step (`output.steps[failed]
  .error` / `.output`) cannot leak a SAS token, bearer, or subscription GUID
  from a sibling failure body (Charter §12).
- `api/services/blast/job_state.py` — `_local_to_blast_job` synthesizes the
  same timeline for external-origin synced rows (no `_progress`) driven off the
  row's live status.
- `api/services/job_artifacts.py` — `build_execution_steps_snapshot` does the
  same for the terminal execution-steps snapshot the SPA overlays.
- No frontend change (the projection produces the existing consumed shapes);
  no IaC change.

## Validation

- `uv run pytest -q api/tests/test_external_job_projection.py
  api/tests/test_job_artifacts.py api/tests/test_local_to_blast_job.py
  api/tests/test_external_blast_api.py api/tests/test_blast_tasks.py
  api/tests/test_blast_jobs_routes.py` — 291 passed, including new external
  step-projection, synced-row, and snapshot regression tests.
- `uv run ruff check api` — clean.
- `cd web && npm run build` — clean (contract typechecks against the existing
  `BlastExecutionStepsSnapshot` / step types).
