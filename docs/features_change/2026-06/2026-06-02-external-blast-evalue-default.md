---
title: External BLAST API evalue default aligned to NCBI (0.05)
description: >-
  The /api/v1/elastic-blast/submit OpenAPI facade now defaults `options.evalue`
  to 0.05 (the NCBI Web BLAST megablast default) instead of 10, matching the
  dashboard submit form so programmatic and UI submissions behave identically.
tags:
  - blast
  - user-guide
---

# External BLAST API evalue default aligned to NCBI (0.05)

## Motivation

Result parity with NCBI Web BLAST depends on the request-side options matching
NCBI's defaults. The dashboard submit form already defaulted the expect-value
(`evalue`) threshold to **0.05** — the NCBI Web BLAST megablast default — and
its preset table labels 0.05 as "Standard (default)". The external OpenAPI
submit facade (`POST /api/v1/elastic-blast/submit`), however, still defaulted
`options.evalue` to **10**, so a programmatic caller who omitted `evalue` got a
much looser threshold than an equivalent UI submission, producing a longer,
non-NCBI-equivalent hit tail.

## User-facing change

`ExternalBlastOptions.evalue` now defaults to **0.05** instead of 10. Callers of
`/api/v1/elastic-blast/submit` that omit `options.evalue` now get the same
threshold as the dashboard form and NCBI Web BLAST. Callers that pass an
explicit `evalue` are unaffected. The field validation (`gt=0`) is unchanged.

## API / IaC diff summary

- `api/routes/elastic_blast.py` — `ExternalBlastOptions.evalue` default
  `10.0` → `0.05`, with a description noting the NCBI / dashboard alignment.

No other endpoint changed. The result-analytics post-filter default
(`max_evalue=10.0` in `api/routes/blast/result_analytics.py`) is a separate
display filter on already-computed hits and is intentionally left unchanged.

## Validation evidence

- `uv run pytest -q api/tests/test_external_blast_api.py` — new
  `test_external_blast_options_default_evalue_matches_ncbi` asserts an omitted
  `options` block yields `evalue == 0.05`; existing forwarding tests (which
  pass `evalue` explicitly) stay green.
- `uv run pytest -q api/tests/test_blast_submit_route_options.py` — UI/OpenAPI
  shared-execution-config tests green.
- `uv run pytest -q api/tests` — full suite green (one unrelated
  `test_terminal_exec` parallel-timing flake passed on isolated re-run).
- `uv run ruff check api/routes/elastic_blast.py`.
