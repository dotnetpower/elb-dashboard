# Field-aware sharded tabular merge (taxid / reordered outfmt)

## Motivation

To surface subject taxids + names in BLAST results, the recommended layout is a
tabular outfmt with extended/reordered columns, e.g.

```
-outfmt "7 sseqid staxids sstrand pident evalue bitscore qstart qend sstart send qseq sseq"
```

The shard merge re-ranks each query's hits across shards by evalue/bitscore and
applies the per-query `max_target_seqs` cutoff. It historically assumed the
BLAST `std` column order — qseqid=0, sseqid=1, evalue=10, bitscore=11. The
layout above breaks every one of those positions (sseqid=0, staxids=1, evalue=4,
bitscore=5, and no qseqid at all), so the merge would silently rank on the wrong
columns. This change makes the merge resolve those columns **by name** so any
ordering merges correctly while preserving the extended columns.

## Scope (gate stays CLOSED — zero production impact)

This implements the merge capability only. The FE / backend / elastic-blast
gates still admit only `5`, `6`, `6 std…`, `7`, `7 std…`, so a reordered/extended
layout cannot reach the merge from a production submit yet. The capability is
exercised in isolation by the subprocess merge tests. Enabling the end-to-end
path additionally requires (separately, after live verification):

- the vendored elastic-blast option-injection fixes (YAML-safe env + quote-aware
  shell argv in `blast-run-aks.sh`) so a multi-token `-outfmt` survives to each
  shard pod;
- the dashboard normalising the submitted outfmt to include `qseqid` first (see
  Known limitation), then opening the gates.

## API / IaC diff summary

`terminal/merge-sharded-results.sh`:

- New `parse_outfmt_spec()` returns the FULL `-outfmt` value (vs `parse_outfmt`
  which returns just the leading code for dispatch).
- New `expand_outfmt_fields()` / `resolve_tabular_columns()` resolve the group
  (qseqid), rank (evalue, bitscore) and oracle (subject accession) column
  indices by field name; `std` expands to the standard 12 codes.
- `merge_tabular(...)` takes `outfmt_spec`, resolves the columns, and re-ranks /
  groups / runs the tie-order oracle by the resolved indices.
  `tabular_subject_accession(line, subject_idx)` is now index-parameterised.
- Fail-closed when evalue/bitscore are absent (`ValueError`). No query column →
  single-group fallback + warning. No subject column → oracle/deterministic
  tie-break disabled + warning. The report gains a `resolved_columns` block.
- A plain or `std`-prefixed layout resolves back to the historical positions, so
  existing runs are byte-identical.

No backend route / Bicep changes.

## Known limitation

A layout without a query column (the guide's exact example) merges every hit as
one query group — correct only for single-query searches, and the dashboard
tabular analytics also key on `qseqid`. Before the production gate opens, the
dashboard must normalise the submitted outfmt to include `qseqid` (first), so
both the merge and the read-side aggregation stay per-query correct.

## Validation evidence

- `api/tests/test_sharded_merge.py` (subprocess, runs the real script):
  - `…_reordered_fields_taxids` — the guide's exact 12-field reordered layout
    (sseqid first, staxids col1, evalue col4, no qseqid) ranks correctly by the
    resolved columns, preserves staxids, reports `resolved_columns` + the
    single-group warning.
  - `…_extended_fields_header` (`7 std staxids …`) and the plain outfmt 6/7
    tests stay green (byte-identical std path).
  - `…_rejects_outfmt_without_rank_columns` — missing evalue/bitscore fails
    closed (non-zero exit, "evalue and bitscore" message).
- `uv run pytest -q api/tests/test_sharded_merge.py -m ''` — 14 passed.
- `uv run pytest -q api/tests -m '' -k "shard or merge or sharding or precision
  or web_blast or parity"` — 242 passed, 3 skipped.
- `uv run ruff check api/tests/test_sharded_merge.py` — passed; embedded merge
  python `ast.parse` clean.

## Deploy note

The merge script ships in the terminal / OpenAPI build context, so it takes
effect on the next image rebuild. It changes no behaviour for current
(std-layout) runs; the new code paths are reachable only once the gates open.
