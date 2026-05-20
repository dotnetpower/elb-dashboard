# BLAST Submit NCBI-Style UX Refresh

## Motivation

Researchers coming from NCBI Web BLAST expect the submit flow to move through program choice, query entry, search set selection, taxonomy scoping, task optimization, advanced algorithm parameters, and then submission. The existing page already had the underlying ElasticBLAST/Azure controls, but some of the flow was more infrastructure-oriented than Web BLAST-like.

## User-Facing Change

- Kept the top-level BLAST program tabs first, matching the Web BLAST mental model.
- Moved `blastn` task optimization into its own `Program Selection` section after query/search/taxonomy, with NCBI-style `megablast`, `dc-megablast`, and `blastn` choices plus parameter previews.
- Added search-set category tabs for standard, rRNA/ITS, genomic/transcript, and custom databases while preserving storage-backed database selection and warm-state labels.
- Renamed the compute block to `Execution Profile` and added researcher-facing run profiles: baseline, warmed database, and sharded throughput.
- Reworked advanced parameters into NCBI-style groups: General Parameters, Scoring Parameters, and Filters and Masking.
- Added Web BLAST-inspired controls for automatic short-query adjustment, culling limit, soft masking, lower-case masking, and species-specific repeat masking.
- Made the compact submit action bar sticky so readiness and submit actions stay close while reviewing long parameter forms.

## API / IaC Diff Summary

- No backend route or IaC changes.
- Frontend submit payload construction now maps the new structured controls into BLAST CLI `additional_options`:
  - `-task blastn-short` for short `blastn` queries when automatic adjustment is enabled.
  - `-culling_limit` for max matches in a query range.
  - `-soft_masking true` for lookup-table-only masking.
  - `-lcase_masking` for lower-case query masking.
  - `-window_masker_taxid` for species-specific repeat masking.
- Config export / duplicate hydration now round-trips the new advanced controls.

## Validation Evidence

- `cd web && npm run test -- blastSubmit` — 8 files, 115 tests passing.
- `cd /home/moonchoi/dev/elb-dashboard/web && npm run build` — TypeScript and Vite production build succeeded. Vite emitted the existing large-chunk warning only.
- Browser smoke at `http://127.0.0.1:8090/blast/submit` — the refreshed NCBI-style submit order rendered through Program, Query, Search Set, Taxonomy, Program Selection, Execution Profile, and Algorithm Parameters; screenshot captured during validation.
