# BLAST result database metadata

## Motivation

Researchers need to confirm the exact BLAST database provenance from the search result page, similar to the NCBI BLAST report details panel.

## User-facing change

The BLAST result header now shows database details when available: title, description, molecule type, update date, sequence count, letter count, and snapshot version. The `core_nt` description is populated from the built-in NCBI database catalogue, while dynamic counts and dates are read from workload Storage metadata.

## API / IaC diff summary

- `GET /api/blast/jobs/{job_id}` includes an optional `database_metadata` object for single-job detail reads.
- Job list polling does not include this enrichment, avoiding per-row Storage metadata lookups.
- Storage database listing preserves BLAST `.njs` display metadata fields when present.
- No IaC changes.

## Validation evidence

- `uv run pytest -q api/tests/test_blast_db_metadata.py api/tests/test_storage_data.py api/tests/test_local_to_blast_job.py` — 33 passed.
- `uv run ruff check api/services/blast_db_metadata.py api/services/storage_data.py api/services/blast_job_state.py api/routes/blast/jobs.py api/tests/test_blast_db_metadata.py api/tests/test_storage_data.py api/tests/test_local_to_blast_job.py` — passed.
- `cd web && npm run build` — passed. Vite reported the existing large chunk warning.
- `curl --max-time 20 http://127.0.0.1:8085/api/blast/jobs/eb5771a0-b20f-437b-a21f-ec62670c1bdf` — returned `database_metadata` with the detailed `core_nt` description, molecule type, update date, sequence count, letter count, and snapshot.
- Browser snapshot of the local result page confirmed the header renders the DB metadata rows without overlap.
- `uv run pytest -q api/tests` — 727 passed.
- Production test deployment completed with image tag `20260520013810`; `https://ca-elb-control.gentlemeadow-01289e5b.koreacentral.azurecontainerapps.io/api/health` returned 200 from revision `ca-elb-control--0000101`.
