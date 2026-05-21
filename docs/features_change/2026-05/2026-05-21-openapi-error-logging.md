# OpenAPI Error Logging

## Motivation

Operators could see that the external ElasticBLAST OpenAPI status endpoint returned HTTP 400, but the worker logs did not include the response body or enough request context to explain the failure.

## User-facing change

OpenAPI upstream HTTP failures now emit warning logs with method, sanitized URL, status code, reason phrase, selected request id headers, and a sanitized response detail snippet. The stale job reconciler also logs the dashboard job id, Azure scope, HTTP status code, and exception detail when external refresh fails.

## API/IaC diff summary

- `api.services.external_blast` logs sanitized upstream HTTP error details before translating them to `HTTPException`.
- `api.tasks.blast.reconcile_stale_jobs` promotes external refresh failures from debug type-only logging to warning logs with detailed context.
- No API contract or IaC changes.

## Validation evidence

- `uv run pytest -q api/tests/test_external_blast_api.py api/tests/test_blast_tasks.py` — 135 passed.
- `uv run ruff check api/services/external_blast.py api/tasks/blast/__init__.py api/tests/test_external_blast_api.py api/tests/test_blast_tasks.py` — passed.