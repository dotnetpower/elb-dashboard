# Frontend Taxonomy Filter

## Motivation

Researchers need to apply the backend-supported BLAST taxonomy filter from the browser submit form without manually typing BLAST+ flags into Additional options.

## User-facing change

The BLAST submit page now includes a Taxonomy Filter section. Users can search NCBI Taxonomy by organism name or taxid, select a result, enter a manual taxid fallback, choose include-only or exclude mode, and see the resulting `-taxids` or `-negative_taxids` flag in the command preview.

The search field starts a short debounced lookup for valid queries, prevents oversized queries before they hit the backend, and surfaces invalid manual taxids inline. The submit request builder also rejects invalid or conflicting taxonomy state as a final guard.

The form warns that taxonomy filtering requires the selected BLAST database to include taxonomy metadata and blocks conflicting `-taxids` / `-negative_taxids` flags in Additional options.

## API / IaC diff summary

- Added a typed frontend client for `GET /api/blast/taxonomy/search?q=<query>&limit=<limit>`.
- Added `taxid` and `is_inclusive` to the BLAST submit request payload when a positive integer taxid is selected.
- Added no IaC changes.

## Validation evidence

- `cd web && npm run test -- taxonomyFilter.test.ts` - 8 passed.
- `npm run build` from `web/` - passed; Vite reported the existing large chunk warning.
- Browser check at `http://127.0.0.1:8090/blast/submit` - Taxonomy Filter section rendered between Database and Compute; invalid manual taxid `0` displayed an inline warning; manual taxid `9606` displayed the selected filter chip.
