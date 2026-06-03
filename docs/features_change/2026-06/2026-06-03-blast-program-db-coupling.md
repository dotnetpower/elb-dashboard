# BLAST program ↔ database coupling and NCBI v5 catalog name fixes

## Motivation

On the BLAST submit page the program picker and the database picker were only
loosely coupled. A researcher could switch to a program whose molecule type had
no downloaded database, or keep an incompatible database selected after changing
the program, and only discover the mismatch at submit-time validation. We also
needed to confirm that the per-program databases NCBI exposes in Web BLAST are
actually pullable through our `elastic-blast` (BLAST+ 2.17 / v5) pipeline.

## User-facing change

1. **Program change overwrites the database to match the molecule type.** When a
   researcher picks a new program (`blastn` / `tblastn` / `tblastx` are
   nucleotide; `blastp` / `blastx` are protein), the current database selection
   is reconciled:
   - kept if it is already a ready database of the right molecule type (or its
     type cannot be classified),
   - overwritten with a ready database of the required molecule type if the
     current one is incompatible or empty,
   - left unchanged (program click blocked) when no ready database of the
     required molecule type is downloaded.
2. **Program tabs are blocked when no compatible DB exists.** Tabs without a
   ready database of their molecule type render disabled (`aria-disabled`,
   muted styling) and, on click, raise an info toast — consistent with the
   existing step-gating UX:
   `No <nucleotide|protein> database is downloaded. Prepare one from the
   Dashboard before choosing <program>.`
3. **NCBI v5 catalog name corrections** so the Download button targets real
   prebuilt databases instead of 404ing:
   - `refseq_select` → `refseq_select_rna`
   - `tsa` → `tsa_nt`
   - `pat` → `patnt`

## API / IaC diff summary

Frontend-only. No backend, OpenAPI, or Bicep changes. Backend `db_name`
validation is regex-based (no allowlist), so the renamed values pass unchanged.

- `web/src/pages/blastSubmit/helpers.ts` — `resolveDbMoleculeType`,
  `deriveDbAvailabilityByType`, `ProgramSwitchDecision`, `decideProgramSwitch`.
- `web/src/pages/blastSubmit/types.ts` — narrowed `ProgramSectionProps`
  (`onSelectProgram` + `dbAvailableByType`); new dedicated `OptimizeSectionProps`.
- `web/src/pages/blastSubmit/ProgramSection.tsx` — presentational tab gating.
- `web/src/pages/blastSubmit/OptimizeSection.tsx` — uses `OptimizeSectionProps`.
- `web/src/pages/BlastSubmit.tsx` — `handleProgramSelect`, `dbAvailableByType`.
- `web/src/theme/glass.css` — `.blast-program-tab--blocked` styles.
- `web/src/components/cards/storageDbCatalog.ts` — three v5 name fixes.

## Validation evidence

- `cd web && npm run build` — passes (built in 8.43s).
- `cd web && npx vitest run src/pages/blastSubmit src/components/cards` —
  23 files, 230 tests pass (incl. `programSelection.test.ts` 13 tests,
  `storageDbCatalog.test.ts` 2 tests).
- `npx eslint src/pages/blastSubmit/OptimizeSection.tsx
  src/pages/blastSubmit/types.ts` — clean.
- NCBI v5 FTP (`/blast/db/v5/`) confirmed the authoritative names
  `refseq_select_rna`, `tsa_nt`, `patnt`; the old values do not exist as v5
  prebuilt databases.
