# BLAST Tie-Window Comparator

## Motivation

NCBI Web BLAST and local ElasticBLAST/AKS sharded `core_nt` runs can produce different strict top-N accession order when thousands of hits share the same primary HSP score. The existing comparator only reported strict order equivalence, which made tied-hit subset differences look like biological mismatches.

## User-Facing Change

The Web XML vs outfmt 6 dev comparator now reports `tie_window_equivalent` and a `tie_window` summary. Strict equivalence remains unchanged. Passing `--accept-tie-window` makes the script exit successfully only when strict order fails but every Web row is present in the candidate pool with identical primary HSP values and the Web top-N and candidate top-N share one score class.

Follow-up on 2026-05-18: the comparator can also emit the Web XML accession order with `--write-accessions` and normalized primary-HSP CSV evidence with `--write-normalized-csv`. EQ14 now uses this shared path instead of carrying a separate inline Web XML parser before the strict Web oracle merge.

## API / IaC Diff Summary

No API or IaC changes. The change is limited to `scripts/dev/compare-blast-web-xml-outfmt6.py`, `scripts/dev/eq14-core-nt-webxml-sharded.sh`, focused tests, and equivalence documentation.

## Validation Evidence

- `python scripts/dev/compare-blast-web-xml-outfmt6.py --web-xml docs/temp/f3l-core-nt-2026-05-17/current-web-0k7ge593016.xml --candidate docs/temp/f3l-core-nt-2026-05-17/web-top500-local-status/merged-wide-webmask-dedup.outfmt6.tsv --query-id 'NC_063383.1:c46483-46022' --json docs/temp/f3l-core-nt-2026-05-17/web-top500-local-status/current-web-vs-merged-wide-webmask.tie-window.json --accept-tie-window`
- `docs/temp/f3l-core-nt-2026-05-17/web-top500-local-status/current-web-vs-merged-wide-webmask.tie-window.json` reports `equivalent = false`, `tie_window_equivalent = true`, `shared_accessions = 500`, and `value_mismatch_count = 0`.
- Focused pytest target: `uv run pytest -q api/tests/test_compare_blast_web_xml_outfmt6.py`.
- Follow-up focused pytest on 2026-05-18: `uv run pytest -q api/tests/test_compare_blast_web_xml_outfmt6.py` reported `5 passed` and covers Web XML accession oracle plus normalized CSV emission. Full backend tests `uv run pytest -q api/tests` reported `634 passed`.
