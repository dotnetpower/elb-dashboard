# Canonical BLAST Jobs API

## Motivation

The dashboard had two job surfaces: `/api/blast/jobs` for local Table-backed jobs and `/api/v1/elastic-blast/jobs` for direct OpenAPI submissions. The frontend had to merge both sources and result downloads still depended on blob names or the external-only file route.

## User-facing change

`/api/blast/jobs` is now the canonical job list/status surface. It merges local dashboard jobs with sibling OpenAPI jobs, supports canonical submit at `POST /api/blast/jobs`, and exposes path-based result file downloads via `/api/blast/jobs/{job_id}/results/{file_id}`.

## API / IaC diff summary

- Backend: `/api/blast/jobs` now merges external OpenAPI jobs when available.
- Backend: `/api/blast/jobs/{job_id}` falls back to the external OpenAPI service when local job state is missing.
- Backend: local dashboard submit responses now include both `job_id` and `instance_id` so clients can navigate to the canonical job detail page.
- Backend: result listings return `files` and include deterministic local `file_id` values; external result files keep sibling-generated IDs.
- Backend: added `/api/blast/jobs/{job_id}/results/{file_id}` streaming downloads through the api sidecar without issuing SAS URLs.
- Frontend: removed client-side external jobs merge, sends submissions to `POST /api/blast/jobs`, and switched result downloads to the new `file_id` streaming endpoint.
- IaC: no changes.

## Validation evidence

- `uv run pytest -q api/tests/test_external_blast_api.py api/tests/test_smoke.py` -> 47 passed.
- `uv run pytest -q api/tests` -> 237 passed.
- `cd web && npx tsc --noEmit && npm run build` -> passed; Vite reported the existing large-chunk warning.
- VS Code diagnostics on touched backend/frontend files -> no errors.
