# Sharded merge preserves extended outfmt 7 Fields header

## Motivation

`-outfmt "7 std staxids sstrand qseq sseq"` (std-prefixed extended tabular)
already passed the shard-merge gates and ran sharded, because forcing `std` as
the first field keeps the standard 12 columns at fixed positions so the merge
re-ranks correctly by `evalue`/`bitscore`. The merge also re-emits each row
verbatim, so the trailing extended columns (taxids, strand, query/subject seq)
were preserved in the data.

The one defect: the merged output's `# Fields:` comment header was hardcoded to
the bare std 12 fields, so the extended columns were mislabelled — a downstream
consumer reading `# Fields:` (the dashboard tabular parser does exactly this)
would not know columns 13-16 were `subject taxids, subject strand, query seq,
subject seq`.

## User-facing change

A sharded run with extended outfmt 7 fields now produces a merged file whose
`# Fields:` header matches the actual columns, so taxid / strand / sequence
columns are correctly described and parse end-to-end. Web BLAST equivalence is
unchanged — this only fixes header labelling, not hits, scores, or ranking.

## API / IaC diff summary

- `terminal/merge-sharded-results.sh` (`merge_tabular`): capture the first
  authoritative `# Fields:` line BLAST itself wrote into the shard outputs and
  reuse it verbatim in the merged output, falling back to the standard 12-field
  string when the input carries no comment header (plain outfmt 6 — unchanged
  behaviour). Using BLAST's own header avoids any specifier→label table drift.
  The tabular report now also carries a `fields` key for observability.

No backend/route/IaC changes. The merge still re-ranks by the fixed std
positions (`cols[0]`/`[10]`/`[11]`), which the gate guarantees by requiring
`std` to be the first field — extended layouts that do not lead with `std` stay
blocked.

## Validation evidence

- New `api/tests/test_sharded_merge.py::test_merge_sharded_results_outfmt7_extended_fields_header`
  runs the real `merge-sharded-results.sh` with `-outfmt "7 std staxids sstrand
  qseq sseq"`: 16-column rows preserved + re-ranked, merged `# Fields:` equals
  the extended header, report `fields` records it.
- `uv run pytest -q api/tests/test_sharded_merge.py -m ''` — 12 passed.
- `uv run pytest -q api/tests -m '' -k "shard or merge or sharding or precision
  or config_sharding"` — 202 passed.
- `uv run ruff check api/tests/test_sharded_merge.py` — passed; embedded merge
  python `ast.parse` clean.

## Deploy note

Takes effect after the terminal image is rebuilt (the merge script ships in the
terminal/OpenAPI build context). To emit taxid under sharding while staying Web
BLAST-equivalent, combine `sharding_mode=precise` (search-space correction +
tie-order oracle) with `-outfmt "7 std staxids ..."`.
