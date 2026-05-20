# Command Preview Search Space

## Motivation

The New Search command preview did not show the verified `-searchsp` value used for Web BLAST-compatible `core_nt` output, even though the submit path included it.

## User-facing change

The command preview now displays `-searchsp <value>` when the selected database exposes a verified Web BLAST search-space default. If the user explicitly enters `-searchsp` in Additional options, the preview preserves that override and does not duplicate the flag.

## API and IaC diff summary

No API or IaC changes. The frontend preview now receives the existing database metadata field `web_blast_searchsp` and renders it in the preview command.

## Validation evidence

- `cd web && npm run test -- taxonomyFilter.test.ts --run`
- `cd web && npm run build`
- Backend submit/config tests already cover actual `db_effective_search_space` and Web BLAST default injection into `-searchsp`.