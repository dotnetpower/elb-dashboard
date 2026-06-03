---
title: Fix precise-sharding BLAST submit failing for NCBI accession queries
description: Accession-only submits with precise sharding no longer 422 with "precise sharding requires query metadata".
tags:
  - blast
  - user-guide
---

# Fix precise-sharding BLAST submit for NCBI accession queries

## Motivation

Submitting a BLAST job with an NCBI accession (the "Or fetch by NCBI
accession" path, "Will fetch at submit") and **precise** sharding failed with:

> Submission failed: precise sharding requires query metadata

The accession (e.g. `OZ254605.1`) is only resolved to FASTA later in
`_normalise_blast_submit_body`, but the pre-side-effect precision contract
(`submit_contracts` → `_canonical_query_from_body`) ran first on the raw body.
It only recognised inline `query_data` / `query_file`, so an accession-only
submit reported `query_count = None`, and `build_precision_report` blocked
precise sharding before the accession was ever fetched.

## User-facing change

Accession-only submits with precise sharding now pass the readiness/contract
gate and queue normally. An NCBI nuccore accession resolves to exactly one
FASTA record (a subrange narrows that record but does not change the count), so
the canonical query is reported as a single query (`query_count = 1`).

## API / IaC diff summary

- `api/services/blast/submit_payload.py` — `_canonical_query_from_body` now
  recognises a `query_accession`-only body and returns
  `{"kind": "ncbi_accession", "accession": …, "query_count": 1}`. This feeds
  both `submit_contracts` (the pre-fetch precision gate) and
  `canonical_submit_snapshot`. No change to the inline-FASTA / `query_file`
  precedence, and the mixed-source conflict is still rejected at normalise.
- No IaC change.

## Validation evidence

- `uv run pytest -q api/tests/test_blast_submit_accession.py api/tests/test_blast_compatibility.py` → 20 passed
- New tests: `test_submit_contracts_precise_sharding_allows_accession_query`,
  `test_canonical_query_reports_single_count_for_accession`.
- Wide sweep: `uv run pytest -q api/tests -k "submit or precision or sharding or accession or compatibility or provenance"` → 306 passed
- `uv run ruff check api/services/blast/submit_payload.py api/tests/test_blast_submit_accession.py` → clean
