# Trusted BLAST Submit Metadata

Date: 2026-05-20

## Motivation

Dashboard and external BLAST submissions need stable server-derived metadata so retry/recovery and future OpenAPI delivery can distinguish trusted job origin from caller-supplied fields.

## User-Facing Change

- Dashboard submits now persist `submission_source=dashboard` and `external_correlation_id=<dashboard job id>` in the job payload.
- External submit routes now forward `submission_source=external_api` with a server-generated `external_correlation_id`.
- Caller-supplied `submission_source` and `external_correlation_id` values are ignored.
- Valid `idempotency_key`, `priority`, and `resource_profile` fields are preserved for later queue/retry handling.

## API / IaC Diff Summary

- Added `canonical_submit_metadata()` in `api/services/blast_submit_payload.py`.
- Applied trusted metadata in dashboard submit normalization and external submit routes.
- Added tests for dashboard normalization, `/api/v1/elastic-blast/submit`, and canonical `/api/blast/jobs` external submit.
- No IaC change.

## Validation Evidence

- `uv run ruff check api/services/blast_submit_payload.py api/routes/elastic_blast.py api/routes/blast/submit.py api/tests/test_blast_submit_route_options.py api/tests/test_external_blast_api.py` -> passed.
- `PYTHONPATH=$PWD uv run pytest -q api/tests/test_blast_submit_route_options.py api/tests/test_external_blast_api.py::test_external_blast_submit_forwards_contract api/tests/test_external_blast_api.py::test_canonical_jobs_external_submit_uses_trusted_metadata api/tests/test_smoke.py::test_canonical_dashboard_submit_uploads_inline_query` -> 14 passed.
