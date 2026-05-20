# BLAST Jobs List Latency

## Motivation

The `/api/blast/jobs` dashboard poll was taking several seconds on the local control plane. Browser logs showed repeated identical requests, and the backend logs showed 2.5-6.5 second wall times for the list route.

## User-facing change

The BLAST Jobs list route now returns a much smaller summary payload and serves identical short-burst polls from a 5 second in-memory response cache. This keeps the dashboard responsive when multiple cards request the same jobs scope at the same time.

## API diff summary

- The list route reads Table Storage with a summary projection instead of fetching `payload_json` for every row.
- Optional split-child summaries are only queried for rows whose phase indicates a split-query parent.
- External OpenAPI job listing, per-job detail enrichment, cluster endpoint discovery, and external sync checks use short in-memory caches.
- Job submit and delete paths clear the jobs list response cache so state changes appear promptly.

## Validation evidence

- `uv run ruff check api/routes/blast/jobs.py api/routes/blast/submit.py api/services/state_repo.py api/services/blast_job_state.py api/conftest.py`
- `uv run pytest -q api/tests/test_external_blast_api.py api/tests/test_local_to_blast_job.py api/tests/test_state_repo.py`
- Local curl before: scoped `/api/blast/jobs` returned about 215 KB in about 3.6 seconds.
- Local curl after: first scoped miss returned about 10 KB in about 1.1 seconds; repeated identical requests within the cache window returned in about 5-7 ms.