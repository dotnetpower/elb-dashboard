# Submit Compatibility Before Upload

## Motivation

The `/api/blast/jobs` inline FASTA path could attempt query upload before the precision and Web BLAST compatibility contract rejected a known-ineligible request. In local or degraded storage states, that surfaced as a 503 instead of the intended 422 compatibility response.

## User-Facing Change

- Dashboard submit now validates precision and Web BLAST compatibility before inline query upload or queue side effects.
- False-precise requests against unverified databases return the documented `web_blast_compatibility_blocked` 422 response even when storage is unavailable.

## API / IaC Diff Summary

- Added `_validate_submit_contracts()` in `api.routes.blast.submit`.
- The helper runs `submit_contracts()` before `_normalise_blast_submit_body()` can upload inline `query_data`.
- No API schema or IaC changes.

## Validation Evidence

- Focused regression: `uv run pytest -q api/tests/test_smoke.py::test_blast_jobs_submit_blocks_false_precise_with_unverified_database api/tests/test_smoke.py::test_blast_submit_blocks_false_precise_with_unverified_database`.
- Full backend regression: `uv run pytest -q api/tests` -> 786 passed.