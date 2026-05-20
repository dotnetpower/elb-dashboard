# Web BLAST Compatibility Contract

Date: 2026-05-20

## Motivation

The dashboard and API must not imply NCBI Web BLAST-equivalent output unless the request has evidence-backed search-space metadata and a precise sharding strategy. Unknown databases and exploratory sharding need explicit states before a job is queued.

## User-Facing Change

- `/api/blast/pre-flight` now returns a `compatibility` object and a `web_blast_compatibility` check.
- `/api/blast/submit` and the canonical `/api/blast/jobs` path now block false-precise requests for unverified databases before queueing work.
- Explicit approximate sharding remains allowed, but the compatibility contract marks it as approximate with warnings.
- Verified `core_nt` evidence now carries BLAST version, database snapshot, option scope, evidence artifact path, and recalibration trigger metadata.

## API / IaC Diff Summary

- Added `api/services/blast_compatibility.py` for the compatibility contract.
- Extended `api/services/web_blast_searchsp.py` verified defaults with reproducibility metadata.
- Hardened `api/services/sharding_precision.py` so explicit `-searchsp` in `additional_options` participates in the precision gate and conflicts with `db_effective_search_space` are blocked.
- Wired the contract into `api/routes/blast/submit.py` pre-flight and submit validation.
- Updated `web/src/api/blast.ts` with typed compatibility and precision response shapes.
- No IaC change.

## Validation Evidence

- `uv run ruff check api/services/blast_compatibility.py api/services/web_blast_searchsp.py api/services/sharding_precision.py api/routes/blast/submit.py api/tests/test_blast_compatibility.py api/tests/test_smoke.py` -> passed.
- `PYTHONPATH=$PWD uv run pytest -q api/tests/test_blast_compatibility.py api/tests/test_blast_submit_route_options.py api/tests/test_sharding_precision.py api/tests/test_smoke.py::test_blast_preflight_reports_web_blast_compatibility api/tests/test_smoke.py::test_blast_submit_blocks_false_precise_with_unverified_database api/tests/test_smoke.py::test_blast_jobs_submit_blocks_false_precise_with_unverified_database api/tests/test_smoke.py::test_blast_submit_blocks_invalid_precise_sharding_before_queue` -> 43 passed.
- `cd web && npm run build` -> built successfully.

