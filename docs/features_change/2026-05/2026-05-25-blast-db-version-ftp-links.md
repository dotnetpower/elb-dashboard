# BLAST DB Version FTP Links

## Motivation

The BLAST Databases modal displayed downloaded database snapshot versions but did not let users inspect the matching [NCBI BLAST database FTP directory](https://ftp.ncbi.nlm.nih.gov/blast/db/v5/).

## User-facing change

NCBI snapshot version badges in the BLAST Databases modal now open the corresponding database metadata file on the NCBI FTP `v5/` listing in a new browser tab. Unsafe or incomplete database names fall back to the top-level `v5/` listing instead of generating invalid snapshot directories.

## API/IaC diff summary

- Frontend-only change. No API or IaC contract changes.
- Added a guarded URL helper for NCBI BLAST DB v5 FTP metadata/listing links.

## Validation evidence

- `cd web && npx vitest run src/components/cards/storageDbCatalog.test.ts` — 2 passed.
- `cd web && npx tsc --noEmit` — passed.
- `cd web && npx eslint src/components/cards/storage/BlastDbRow.tsx src/components/cards/storageDbCatalog.ts src/components/cards/storageDbCatalog.test.ts` — passed.
- `curl -I -L https://ftp.ncbi.nlm.nih.gov/blast/db/v5/core_nt-nucl-metadata.json` — 200 OK.
- `curl -I -L https://ftp.ncbi.nlm.nih.gov/blast/db/v5/` — 200 OK fallback listing.