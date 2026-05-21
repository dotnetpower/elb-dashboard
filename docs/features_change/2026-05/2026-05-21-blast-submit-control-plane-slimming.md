# BLAST Submit Control Plane Slimming

## Motivation

Successful BLAST runs were still producing avoidable control-plane work around submit. The stale-job reconciler queried the external OpenAPI plane with dashboard UUIDs before an ElasticBLAST runtime job id existed, creating repeated HTTP 400 warnings and extra network calls. Some checkpoint writes also repeated identical status/phase rows without adding progress information.

## User-facing change

BLAST submit progress should feel quieter and lighter. Reconcile no longer asks the external OpenAPI plane about dashboard-only job ids, and empty duplicate state checkpoints are skipped. Detailed submit progress, logs, and terminal state updates are still preserved when they carry new details.

## API/IaC diff summary

- `reconcile_stale_jobs` now calls external OpenAPI job detail only when a real `job-*` ElasticBLAST id is known.
- `_update_state` skips no-detail duplicate phase/status/error checkpoints.
- Duplicate terminal checkpoints still retry artifact finalization, and dashboard row ids are never treated as external OpenAPI job ids.
- No route, frontend, or IaC changes.

## Validation evidence

- `uv run pytest -q api/tests/test_blast_tasks.py::test_reconcile_logs_external_refresh_http_detail api/tests/test_blast_tasks.py::test_reconcile_skips_external_refresh_without_elastic_job_id api/tests/test_blast_tasks.py::test_update_state_skips_identical_empty_checkpoint`: 3 passed.
- `uv run pytest -q api/tests/test_blast_tasks.py`: 98 passed.
- `uv run ruff check api/tasks/blast/__init__.py api/tests/test_blast_tasks.py`: passed.
