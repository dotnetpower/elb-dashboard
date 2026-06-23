---
title: Inject Web-BLAST-parity columns into tabular outfmt (rich outfmt 7 results)
description: When a BLAST submit uses a tabular outfmt (6/7), inject staxids/sscinames/stitle/qcovs at submit time so the dashboard result page populates Description, Scientific name, and Query Cover the same way it does for outfmt 5 (XML). Idempotent, preserves the caller's columns, merge-safe.
tags:
  - blast
  - user-guide
---

# Inject Web-BLAST-parity columns into tabular outfmt

## Motivation

Running a search with a tabular output format (`-outfmt 6` / `-outfmt 7`) left
the result page's **Description**, **Scientific name**, and **Query/HSP Cover**
columns blank. The dashboard's result analytics read those values from named
tabular columns — `stitle` (description), `sscinames` / `staxids` (scientific
name), and `qcovs` (query cover) — but the default tabular layout (`std`, or a
bare `6` / `7`) carries none of them. An `-outfmt 5` (XML) run was rich because
XML embeds that information; a tabular run was not.

## User-facing change

* When a submit uses a tabular outfmt, the dashboard now appends the missing
  result-UI parity columns — `staxids sscinames stitle qcovs` — at submit time.
  A tabular run's result page is now as complete as an XML run's.
* The caller's own columns are preserved and never duplicated (idempotent). A
  bare `7` becomes `7 std staxids sscinames stitle qcovs`; `outfmt 5` (XML) and
  an already-complete layout are untouched.
* Applies to every tabular submit path — New Search (V1), the Service Bus
  Playground send, and an external queue producer — because they all build the
  same `ExternalBlastV1Request`, where the injection lives.
* Side effect: a downloaded tabular result now includes those extra columns
  (generally desirable — it matches NCBI Web BLAST's Description / Scientific
  Name / Query Cover).

## API / IaC diff summary

* `api/services/sharding_precision.py`: new `enrich_tabular_outfmt()` +
  `_PARITY_TABULAR_FIELDS`. Expands a bare `6`/`7` to `std` first so the shard
  merge's required `evalue` + `bitscore` stay present, then appends any missing
  parity column.
* `api/routes/elastic_blast.py`: the `ExternalBlastV1Request` validator calls it
  after the existing shard-merge compatibility check, so both the New Search and
  Service Bus queue paths enrich.
* No IaC change. No new dependency.

## Why it is safe across the pipeline

* **Shard merge** (`terminal/merge-sharded-results.sh`) resolves its group/rank
  columns BY NAME and re-emits the full row, so trailing columns (the injected
  parity fields) are preserved and the `# Fields:` header lists them.
* **Analytics** (`api/services/blast/result_analytics.py`) map columns by name,
  so the injected `stitle` / `sscinames` / `staxids` / `qcovs` populate the
  Description / Scientific name / Query Cover fields.

## Validation evidence

* `uv run ruff check api/services/sharding_precision.py api/routes/elastic_blast.py` — clean.
* `uv run pytest -q api/tests/test_sharding_precision.py` — 53 passed, including
  the new enrich tests (bare/std expansion, column preservation, idempotency,
  XML no-op, merge-compatibility).
* `uv run pytest -q api/tests/test_servicebus_v1_multitoken.py api/tests/test_settings_service_bus.py -k send_preserves api/tests/test_sharded_merge.py api/tests/test_blast_config_sharding.py api/tests/test_web_blast_parity_fixtures.py`
  — green (multitoken / Playground-send / merge / parity assertions updated to
  the enriched outfmt).
* End-to-end through the model: `ExternalBlastV1Request(... outfmt="7")` →
  `blast_options.outfmt == "7 std staxids sscinames stitle qcovs"`; a caller's
  `"7 qseqid sseqid pident evalue bitscore"` → that list + the parity columns.
* Full result-page rendering for a live outfmt 7 run is confirmed after the api
  redeploy.
