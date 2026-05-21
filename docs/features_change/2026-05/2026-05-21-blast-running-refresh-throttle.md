# BLAST Running Refresh Throttle

## Motivation

Running BLAST job detail polling could call the Kubernetes status probe on every
`GET /api/blast/jobs/{job_id}` request. In local monitoring this made each detail
poll take roughly 4-5 seconds while the page was already polling every 5 seconds.

## User-facing change

The job detail page can still poll frequently for Table-backed progress updates,
but the expensive Kubernetes refresh is throttled per running job. This keeps the
page responsive during active searches while allowing Kubernetes terminal-state
detection to refresh periodically.

The Results page also skips optional database metadata enrichment on its repeated
job-detail poll, avoiding a slow storage metadata lookup while the run is active.
If the execution-steps artifact is absent, the page now uses the inline fallback
once instead of polling the slow artifact lookup every five seconds.

## API/IaC diff summary

- Added a short in-process TTL around the running BLAST Kubernetes refresh helper.
- Changed the Results page job-detail query to request the lean job payload.
- Stopped repeated execution-steps polling when the artifact state is `missing`.
- No API contract changes.
- No IaC changes.

## Validation evidence

- `uv run pytest -q api/tests/test_local_to_blast_job.py`
- `uv run ruff check api/services/blast_job_state.py api/tests/test_local_to_blast_job.py`