---
title: NCBI Web BLAST-style Descriptions export
description: Per-subject Descriptions table and plain-text report export formats that mirror the NCBI Web BLAST results layout.
tags:
  - blast
  - user-guide
---

# NCBI Web BLAST-style Descriptions export

## Motivation

Users familiar with the NCBI Web BLAST results page expect a per-subject
"Descriptions" table (Max Score, Total Score, Query Cover, E value, Per. Ident,
Accession, …) rather than the per-HSP hit table the dashboard already exports.
Aggregating parsed HSPs into that layout lets researchers compare dashboard
output against NCBI output line for line.

## User-facing change

Three new entries in the **Download all results** export menu on the BLAST job
results page:

- **NCBI Descriptions (text)** — tab-separated per-subject table with NCBI columns.
- **NCBI Descriptions (CSV)** — the same table as CSV.
- **NCBI Report (text)** — a plain-text report with an ELB provenance header
  (RID, Program, Database, database snapshot) and per-query fixed-width tables.
  The header carries an explicit "Not an NCBI-issued report" compatibility note.

Per-subject aggregation collapses all HSPs of a subject: Max Score = max bitscore,
Total Score = sum of bitscores, Query Cover = union of query ranges over query
length, E value = min e-value, Per. Ident = top-HSP percent identity,
Acc. Len = subject length. Rows sort by query, then descending max score, then
e-value.

## API / IaC diff summary

- New service `api/services/blast/ncbi_report.py` — `aggregate_ncbi_rows(...)`,
  `format_ncbi_hit_table(...)`, `format_ncbi_report_text(...)` (pure functions
  over parsed hits, no Azure SDK).
- `api/routes/blast/results.py` export route now accepts
  `ncbi-hit-table-text | ncbi-hit-table-csv | ncbi-report-text` and streams the
  rendered text through the api sidecar (no Storage URL to the browser).
- `web/src/api/blast.ts` `BlastExportFormat` union and the export menu /
  format-label switch extended with the three new formats.

## Validation evidence

- `uv run pytest -q api/tests/test_blast_ncbi_report.py` — 9 passed (aggregation
  math, sorting, missing taxonomy, column set, report header, no storage URLs,
  HTTP export of all three formats).
- `cd web && npm run build` — clean.
- `cd web && npm test -- --run` — 454 passed.
