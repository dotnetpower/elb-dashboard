# 2026-05-20 — BLAST results relevance sort

## Motivation

NCBI Web BLAST's Descriptions table does not behave like a plain single-column sort in the default view. Researchers expect tied E-values, especially `0.0` rows, to keep the strongest matches first by score and subject-level total score.

## User-facing change

The BLAST Descriptions / Alignments data query now defaults to `Best match` sorting. The server ranks hits by an NCBI-style composite key:

1. best E-value, ascending
2. max bit score, descending
3. total bit score for the same query + subject, descending
4. query coverage, descending
5. identity, descending
6. alignment length, descending

Explicit column sorts still work as before for E-value, bit score, identity, HSP cover, and length.

## API / IaC diff summary

* `/api/blast/jobs/{job_id}/results/alignments` accepts `sort_by=relevance` and uses it as the default.
* No IaC changes.

## Validation evidence

* Added `test_alignments_default_sort_uses_ncbi_style_relevance_tiebreakers` covering equal-E-value ties ordered by max and total bit score.
* `PYTHONPATH=$PWD uv run pytest -q api/tests/test_blast_results_routes.py api/tests/test_route_contracts.py` — 36 passed.
* `uv run ruff check api/services/blast_result_analytics.py api/routes/blast/results.py api/tests/test_blast_results_routes.py` — passed.
* `cd web && npm run build` — passed.