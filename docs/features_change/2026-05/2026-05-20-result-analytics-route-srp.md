# Result Analytics Route SRP

## Motivation

`api.routes.blast.results` mixed result listing, file preview, downloads, exports, artifact helpers, and analytics endpoints. The route file was difficult to review because `/results/alignments` and `/results/taxonomy` carried most of the parsing and filtering logic.

## User-Facing Change

- No intentional API or UI behavior change.
- Existing result analytics paths remain unchanged:
  - `/api/blast/jobs/{job_id}/results/alignments`
  - `/api/blast/jobs/{job_id}/results/taxonomy`

## API / IaC Diff Summary

- Split analytics routes into `api.routes.blast.result_analytics`.
- Split shared artifact/default-request/blob-validation helpers into `api.routes.blast.result_helpers`.
- Kept result listing, file preview, download, export, and file-id streaming routes in `api.routes.blast.results`.
- Registered the analytics router before the result file-id route to preserve static route precedence.
- No IaC changes.

## Validation Evidence

- Result route focused tests: `uv run pytest -q api/tests/test_blast_results_routes.py api/tests/test_blast_result_manifest.py api/tests/test_job_artifacts.py api/tests/test_smoke.py` -> 101 passed.
- Backend lint: `uv run ruff check api/routes/blast/__init__.py api/routes/blast/results.py api/routes/blast/result_helpers.py api/routes/blast/result_analytics.py` -> passed.
- Full backend regression: `uv run pytest -q api/tests` -> 786 passed.
- VS Code diagnostics on changed Python modules -> no errors.