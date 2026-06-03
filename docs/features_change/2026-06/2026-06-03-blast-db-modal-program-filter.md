---
title: BLAST Databases modal — program-oriented molecule filter and best get path
description: The standalone Storage-card BLAST Databases modal now filters by molecule type (program-oriented), hides non-pullable databases by default, and flags a recommended starter per molecule type.
tags:
  - ui
  - blast
---

# BLAST Databases modal — program-oriented filter + best get path

## Motivation

The standalone BLAST Databases modal (opened from the Dashboard Storage card)
listed every catalog entry flat, with no program context. A researcher who only
runs `blastn` had to mentally map programs → molecule type → the right
database, and the long list mixed in databases NCBI does **not** publish as a
pullable BLAST DB (v4-only, no prebuilt, or too large), so the "Get" action was
not actually available for several rows. There was also no signal for which
database to pull first.

## User-facing change

- **Molecule-type filter tabs** (All / Nucleotide / Protein) at the top of the
  modal. Each non-"All" tab carries a sub-label mapping the molecule type to its
  BLAST programs, so the choice is program-oriented:
  - Nucleotide · `blastn · tblastn · tblastx`
  - Protein · `blastp · blastx`
- **Non-pullable databases are hidden by default.** Only databases the
  server-side NCBI S3 copy can actually `Get` are shown. A "Show unavailable
  (N)" checkbox (with a live hidden count for the active filter) reveals the
  rest, which keep their existing "unsupported" badge + source link.
- **Recommended starter badge.** One curated, broadly useful, pullable database
  per molecule type is flagged with a green "Recommended" badge so the catalog
  points at the best get path instead of leaving the user to guess:
  `core_nt` (nucleotide) and `swissprot` (protein).
- Category sections with no matching databases under the current filter are
  skipped, and an empty-state message appears if a filter combination matches
  nothing.

## API / IaC diff summary

Frontend-only. No backend, route, or Bicep change. The DB catalog gained two
optional fields/exports and two pure helpers; no payload contract changed.

- `web/src/components/cards/storageDbCatalog.ts`
  - `BlastDbCatalogItem.recommended?: boolean` (optional, additive)
  - `export const MOLECULE_PROGRAMS` (nucl/prot → program list)
  - `export type MoleculeFilter = "all" | "nucl" | "prot"`
  - `filterDbCatalog(catalog, moleculeFilter, showUnavailable)` and
    `countUnavailableDbs(catalog, moleculeFilter)` pure helpers
  - `core_nt` and `swissprot` marked `recommended: true`
- `web/src/components/cards/storage/BlastDbModal.tsx` — filter tab bar,
  show-unavailable toggle, filtered category loop, empty-state.
- `web/src/components/cards/storage/BlastDbRow.tsx` — Recommended badge.
- `web/src/theme/glass.css` — `.db-filter-tabs` / `.db-filter-tab` styles.

## Validation evidence

- `cd web && npx vitest run src/components/cards/storageDbCatalog.test.ts` —
  8 passed (added: `filterDbCatalog` nucl/prot/showUnavailable,
  `countUnavailableDbs`, recommended-starter, `MOLECULE_PROGRAMS`).
- `cd web && npx vitest run src/components/cards src/pages/blastSubmit` —
  236 passed across 23 files (was 230; +6 new).
- `cd web && npm run build` — clean.
- `npx eslint` on the four touched files — clean.
