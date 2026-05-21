# API Reference Guide Polish

## Motivation

The API Reference user guide needed to explain the live OpenAPI page as an integration work surface, not only as a screenshot tour. Readers also needed concrete submit, status, and result examples that match the current OpenAPI execution service contract.

## User-Facing Change

- Added a readiness checklist for workspace config, AKS, ACR image, OpenAPI deployment, token configuration, and BLAST database warmup.
- Added a page tour and safer `Try` policy that separates read-only, data-download, mutating, and destructive endpoints.
- Added concrete `POST /v1/jobs`, `GET /v1/jobs/{job_id}/status`, and `GET /v1/jobs/{job_id}/results` examples with expected response shapes.
- Documented result modes `content=full`, `content=merged`, and `content=xml`, including the 404 behavior when merged output is unavailable.
- Added a representative BLAST XML response body for `content=xml`, including the closing tags needed to make the shortened sample structurally complete.
- Updated the submit/status/result examples to use the same `core_nt` monkeypox sample flow as the documented XML output.
- Added external facade examples for `POST /api/v1/elastic-blast/submit`, `GET /api/v1/elastic-blast/jobs/{job_id}`, and `GET /api/v1/elastic-blast/jobs/{job_id}/files/{file_id}`.
- Expanded troubleshooting for token, id mismatch, result availability, queue, and server-error cases.

## API / IaC Diff Summary

- No API changes.
- No infrastructure changes.
- Documentation-only update under `docs/user-guide/api-reference.md`.

## Validation Evidence

- `uv run mkdocs build` passed.
- Local MkDocs server restarted at `http://127.0.0.1:8012/elb-dashboard/`.
- Served page check confirmed the new result and external facade examples are present on `/user-guide/api-reference/`.
- Served page check confirmed the submit/status/result examples use the `core_nt` monkeypox sample and no `16S_ribosomal_RNA` sample remains.