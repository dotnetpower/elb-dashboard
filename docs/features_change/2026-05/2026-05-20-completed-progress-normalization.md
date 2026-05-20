# Completed Progress Normalization

## Motivation

A real local ElasticBLAST run completed successfully while some Run details progress steps still retained `running` in the compact `_progress.steps` payload. The root job state was correct, but stale per-step status could make the completed Run details timeline look active.

## User-Facing Change

When a BLAST job reaches the final `completed` phase, previously running setup and submit steps are normalized to `completed` so the Run details timeline matches the actual job state.

## API/IaC Diff Summary

- Updated `api/tasks/blast/__init__.py` progress payload merging to complete prior running steps when the final completed phase is recorded.
- Added a regression test for final progress normalization.
- No IaC changes.

## Validation Evidence

- `uv run pytest -q api/tests/test_blast_tasks.py -k 'merge_progress_payload_keeps_submit_context_and_live_output or merge_progress_payload_keeps_completed_submit_output or merge_progress_payload_completes_previous_running_steps'` — 3 passed, 79 deselected.
- `uv run ruff check api/tasks/blast/__init__.py api/tests/test_blast_tasks.py` — passed.