# BLAST Result Access Hardening

## Motivation

Authenticated result endpoints could read or export BLAST output by `job_id` and blob name without first proving that the caller owns the job. The Jobs list route already filtered by owner, but detail, delete, cancel, and result routes still had inconsistent Durable entity-state handling.

## User-facing change

BLAST job detail, delete, cancel, result listing, result download, aggregate, alignment, and export routes now require the caller to own the requested job. Result blob downloads are limited to blobs under the requested job's result prefix.

## API/IaC diff summary

- Added shared job registry normalization for legacy list state and object state with a `jobs` list.
- Added strict safe-character validation for `job_id` route parameters.
- Added owner checks before job detail, delete, cancel, result list, download, aggregate, alignment, and export operations.
- Result blob downloads now require `blob_name` to start with `{job_id}/`.
- Custom DB build now validates title characters and caps inline FASTA input at 20 MB before any storage or terminal work starts.
- Added focused regression tests for registry normalization, job id validation, and result blob prefix checks.

## Validation evidence

- `ruff check api/routes/blast_jobs.py api/tests/test_blast_jobs_hardening.py` passed.
- `pytest -q api/tests/test_blast_jobs_hardening.py api/tests/test_models.py api/tests/test_passwords.py api/tests/test_sanitise.py` passed with 16 tests.
- `ruff check api/routes/blast_jobs.py api/routes/blast_tools.py api/services/network.py api/services/compute.py api/services/ssh_exec.py api/tests/test_blast_jobs_hardening.py` passed.
- `python -m py_compile api/routes/blast_jobs.py api/routes/blast_tools.py api/services/network.py api/services/compute.py api/services/ssh_exec.py` passed.
- Deployed production package `funcapp-202605140105.zip`; `/api/health` returned HTTP 200 after restart.
- SWA-origin validation returned HTTP 200 for `/api/blast/jobs`, HTTP 200 for an owned job's result list, and HTTP 400 for a download request using a blob outside the job prefix.
