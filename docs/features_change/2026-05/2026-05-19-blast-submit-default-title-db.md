# BLAST Submit Default Title And Database

## Motivation

New Search users often start from the bundled FASTA examples and then submit multiple similar jobs. The previous generated title omitted creation time, and the Database field started blank even though `core_nt` is the expected default nucleotide database.

## User-facing change

Generated Job Title values now start with a local `yyyymmdd-hhmm` timestamp. New Search also defaults the Database field to `blast-db/core_nt/core_nt`.

## API / IaC / deployment diff

- No API or IaC changes.
- Frontend form defaults now select `core_nt` for new BLAST searches.
- Example-loaded titles and blank-title submission fallbacks use a shared timestamped-title helper.
- Restored drafts with an empty or malformed database fall back to the `core_nt` default.

## Validation

- `npx vitest run src/pages/blastSubmit/useDraftForm.test.ts src/pages/blastSubmit/blastSubmitModel.test.ts` - 11 passed.
- `npm run build` - TypeScript + Vite production build succeeded.