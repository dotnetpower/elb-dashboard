# BLAST Config Storage URL Hardening

## Motivation

Generated ElasticBLAST configs could mix the selected `azure-storage-account` with absolute blob URLs from another Storage account. That can route DB, query, or result traffic outside the configured private endpoint and RBAC boundary.

## User-facing change

BLAST submit and config preview now reject absolute Azure Blob URLs that do not belong to the selected Storage account. Database URLs must point to the `blast-db` container, query URLs must point to the `queries` container, and SAS/query-string URLs are rejected before a job is queued.

## API/IaC diff summary

API-only hardening. No IaC changes.

- Added selected-storage validation in BLAST config URL normalization.
- Added lower-level `generate_config` validation for direct config generation.
- Submit requests now fail with HTTP 422 before queueing when DB/query URLs target another Storage account.
- Config preview fallback now returns HTTP 422 for invalid stored payloads instead of misclassifying them as Storage read failures.

## Validation evidence

- `uv run pytest -q api/tests/test_blast_config_sharding.py api/tests/test_blast_tasks.py api/tests/test_smoke.py::test_blast_submit_rejects_storage_account_mismatch_before_queue api/tests/test_smoke.py::test_blast_job_file_config_preview_rejects_storage_account_mismatch api/tests/test_smoke.py::test_blast_job_file_generates_config_preview_when_blob_missing`
- `uv run ruff check api/services/storage_url_validation.py api/services/blast/task_config.py api/services/blast_task_config.py api/services/blast_config.py api/_http_utils.py api/routes/blast/results.py api/tests/test_blast_tasks.py api/tests/test_blast_config_sharding.py api/tests/test_smoke.py`