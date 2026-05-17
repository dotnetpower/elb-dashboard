# Local Job State Endpoint Defaults

## Motivation

Local API and worker processes could start without `AZURE_TABLE_ENDPOINT`, causing `/api/blast/jobs` to return an empty degraded response even after a dashboard submit was accepted.

## User-facing change

`scripts/dev/local-run.sh api`, `worker`, and `beat` now default `AZURE_TABLE_ENDPOINT` and `AZURE_BLOB_ENDPOINT` to the documented local deployment storage account (`elbstg01`) unless explicitly overridden. The local RBAC helper now grants `Storage Table Data Contributor`, and `/api/blast/jobs` reports external OpenAPI failures with the actual detail code instead of the generic `HTTPException` type name.

## API/IaC diff summary

No IaC change. The local development runner now mirrors the Container App environment contract for Azure Table and Blob endpoints. `JobStateRepository` now idempotently prepares missing `jobstate` / `jobhistory` tables when the caller has data-plane table permissions. The `/api/blast/jobs` degraded metadata is more specific for external OpenAPI failures.

## Validation evidence

- `bash -n scripts/dev/local-run.sh scripts/dev/grant-local-rbac.sh`
- `uv run ruff check api/services/state_repo.py api/tests/test_state_repo.py api/tests/test_external_blast_api.py`
- `uv run pytest -q api/tests/test_state_repo.py api/tests/test_external_blast_api.py` -> 23 passed
- `uv run python -m py_compile api/routes/stubs.py api/services/state_repo.py api/tests/test_external_blast_api.py api/tests/test_state_repo.py`
- `curl -sS http://127.0.0.1:8085/api/blast/jobs` -> 1 dashboard job, no `degraded`, external reason `openapi_not_configured`
