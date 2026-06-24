# CSV / TSV formula-injection (CSV injection) defence for result exports

## Motivation

A spreadsheet (Excel / Google Sheets / LibreOffice) treats a cell whose first
character is `=`, `+`, `@`, TAB, or CR as a *formula*. Every BLAST result export
that emits database- or query-influenced text (subject titles, accessions,
taxonomy strings) into CSV/TSV could therefore smuggle a formula that runs when
the file is opened (OWASP "CSV injection" / "formula injection"). The data
sources here are trusted (the NCBI database + the caller's own query), so this is
defence-in-depth rather than a live threat block, but the export path is the
right place to neutralise it once, uniformly.

## User-facing change

* CSV/TSV result downloads now neutralise any cell that would be read as a
  formula by prefixing a single apostrophe (`'`), so it renders as literal text.
  Benign data and typed numbers are unchanged. Applies to:
  * the results export route (`csv` / `tsv` / `hit-table-*` formats),
  * the NCBI-style description hit table (`ncbi-hit-table-*`),
  * the new Service Bus download gateway `?format=csv|tsv` transcode.
* JSON / XML / plain-text exports are untouched (no formula evaluation).

## API / IaC diff summary

* New `api/services/blast/csv_safety.py` — pure `csv_safe_cell` / `csv_safe_row`
  / `csv_safe_cells` helpers. Single responsibility: neutralise leading formula
  triggers in delimited-export cells.
* Wired into the three delimited writers: `_stream_delimited_export`
  ([api/routes/blast/results_export.py](../../../api/routes/blast/results_export.py)),
  `format_ncbi_hit_table` ([api/services/blast/ncbi_report.py](../../../api/services/blast/ncbi_report.py)),
  and `_render_delimited` ([api/services/blast/result_transcode.py](../../../api/services/blast/result_transcode.py)).
* No IaC change.

## Design choice: `-` is intentionally NOT a trigger

OWASP lists `-` as a trigger, but a BLAST alignment `qseq` / `sseq` can
legitimately begin with a gap (`-`) and an unparsed numeric cell can be a
negative value (`-1e-50`). Escaping a leading `-` would corrupt real scientific
data for a near-zero practical gain (the data sources are trusted), so the
trigger set is `= + @` TAB CR. Numeric columns are already typed (int/float) and
never escaped regardless.

## Validation evidence

* `uv run pytest -q api/tests/test_csv_safety.py` — trigger escaping, number /
  benign / gap-leading / negative passthrough, row + positional helpers.
* `uv run pytest -q api/tests/test_result_transcode.py` — `?format=csv` escapes a
  formula-leading subject id (`test_transcode_csv_neutralises_formula_injection`).
* `uv run pytest -q api/tests/test_blast_results_routes.py
  api/tests/test_blast_ncbi_report.py api/tests/test_blast_workflow_export.py` —
  existing exports unchanged for benign data (205 + 30 passed in the wider sweep).
