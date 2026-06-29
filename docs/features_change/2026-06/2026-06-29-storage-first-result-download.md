# Storage-first download for completion-event result files

## Motivation
The completion-event `download_url` gateway tried the AKS-hosted elb-openapi
proxy first and only fell back to Storage after the upstream timed out. When the
cluster is auto-stopped, every download waited ~20s for that timeout before the
Storage fallback ran — even though the result bytes are durably in the workload
Storage account regardless of cluster power state.

## User-facing change
Result-file downloads (`GET /api/v1/elastic-blast/jobs/{job}/files/{file_id}`,
the URL embedded in `result_files[].download_url`) now serve **Storage-first**:
a job with a captured result manifest streams immediately from Storage, with no
dependency on cluster power state and no 20s timeout. Jobs that predate manifest
capture fall back to the elb-openapi proxy (unchanged behaviour for them).

## API / IaC diff
- `api/routes/elastic_blast.py`: `download_external_blast_file` now calls
  `stream_result_file_from_storage` first; on `result_unavailable_offline` (no
  stored manifest/account) it falls back to `stream_file` (openapi proxy). New
  `_is_offline_unavailable` helper; removed the now-unused
  `_is_openapi_unreachable`. No schema change.
- No IaC change.

## Validation
- `uv run pytest -q api/tests/test_external_blast_api.py` — 138 passed.
- New: `test_download_route_serves_from_storage_first`,
  `test_download_route_falls_back_to_openapi_without_manifest`,
  `test_download_route_propagates_non_offline_storage_errors`.
- `uv run ruff check api` — clean.
- Not redeployed (ordinary api code change; validated via pytest).
