# 2026-05-24 — New Search: DB-first stepper + per-DB query examples

## Motivation

On the New Search workspace the stepper ran **Program → Query → Database**. The
default example queries (MPXV, P. falciparum 18S, SARS-CoV-2) only produce hits
against `core_nt` / `nt`, so users picking `16S_ribosomal_RNA`,
`ITS_RefSeq_Fungi`, `pdbnt`, `swissprot`, `nr`, or `refseq_protein` would load
an example, run a search, and see zero hits. Worse, the `recommendedDb` text on
the P. falciparum entries mentioned `18S_fungal_sequences` (not in our
catalogue), which was actively misleading.

## User-facing change

1. **Stepper reorder** — Step 2 is now **Search set**, step 3 is **Query
   sequence**. The Query section is greyed out with a notice until a database
   is picked, so the example picker can always scope to a real DB.
  Switching to a category with downloaded databases now auto-selects the first
  database in that category (for example, `Standard databases` → `core_nt`,
  `rRNA/ITS databases` → `16S_ribosomal_RNA`) so Query unlocks immediately.
2. **Per-DB example filtering** — opening "Load example" now shows only the
   templates that produce hits against the currently selected database. A
   subtitle reports the count, e.g. *"Showing examples that hit
  16S_ribosomal_RNA (5/30)."* Custom or uncovered databases show an empty
  curated-example state instead of falling back to unrelated examples.
3. **Expanded authoritative examples** (NCBI / UniProt / RCSB): every built-in
  DB now has at least five matching examples.
   - `E. coli` 16S rRNA — NR_024570.1 (1,450 nt) → `16S_ribosomal_RNA`, `core_nt`, `nt`
  - Additional 16S rRNA examples: `B. subtilis` NR_102783.2, `S. aureus` NR_037007.2, `P. aeruginosa` NR_117678.1, `M. tuberculosis` NR_044826.2
   - `S. cerevisiae` ITS — NR_111007.1 (752 nt) → `ITS_RefSeq_Fungi`, `core_nt`, `nt`
  - Additional fungal ITS examples: `C. albicans` XR_002086439.1, `A. fumigatus` PZ411633.1, `C. neoformans` PZ094085.1, `P. chrysogenum` PZ394474.1
   - Yeast tRNA-Phe — PDB 1EHZ chain A (76 nt, U→T) → `pdbnt`, `core_nt`, `nt`
  - Additional PDB nucleotide examples: P4-P6 ribozyme PDB 1GID, TPP riboswitch PDB 2GDI, `A. thaliana` TPP riboswitch PDB 3D2V, adenine riboswitch PDB 5SWE
   - Human insulin — UniProt P01308 (110 aa, `blastp`) → `swissprot`, `nr`, `refseq_protein`
   - Human p53 — UniProt P04637 (393 aa, `blastp`) → `swissprot`, `nr`, `refseq_protein`
   - SARS-CoV-2 Spike — UniProt P0DTC2 (1,273 aa, `blastp`) → `swissprot`, `nr`, `refseq_protein`
  - Additional protein examples: human hemoglobin beta P68871 and human cytochrome c P99999
4. **Program auto-switch** — loading a protein example sets
   `program = blastp`; loading a nucleotide example keeps `blastn`. Previously
   every example forced `blastn`.
5. **Fixed misleading `recommendedDb`** on the five P. falciparum 18S entries
   (`18S_fungal_sequences or core_nt` → `core_nt or nt`).
6. **Stale subrange hardening** — loading a new example clears any previous
  `query_from` / `query_to` values so an old subrange cannot silently clip the
  newly loaded sequence.

## API / IaC diff summary

Frontend only. No backend, no infra, no Bicep, no Container App template
changes. No new dependencies.

Touched files:
- `web/src/pages/blastSubmit/queryExamples.ts` — added `matchingDbs: string[]`
  and `blastProgram: BlastProgram` fields on the template type; populated for
  all 30 templates (10 existing migrated, 20 new added across NCBI / UniProt /
  RCSB sources).
- `web/src/pages/blastSubmit/queryExamples.test.ts` — new assertions that every
  template tags a known program + at least one known DB, that every key in
  `DB_DESCRIPTIONS` is covered by at least one template, and that nucleotide
  templates only point at nucleotide DBs (and likewise for protein). Also pins
  the selected-DB-only filter counts for `16S_ribosomal_RNA`,
  `ITS_RefSeq_Fungi`, `pdbnt`, `swissprot`, unknown custom DBs, and empty DB
  selection, and enforces at least five examples for every built-in DB.
- `web/src/pages/blastSubmit/DatabaseSection.test.ts` — pins category
  auto-selection paths for Standard, rRNA/ITS, Genomic, and Custom database
  buckets, including empty-category behaviour.
- `web/src/pages/blastSubmit/SubmitStepper.tsx` — swapped `STEPS[1]` and
  `STEPS[2]`, updated `stepDone()` case 2/3 mapping.
- `web/src/pages/blastSubmit/DatabaseSection.tsx` — `SectionHeader step={3}` →
  `step={2}`; category tab changes now auto-select the first downloaded
  database in the selected category unless the current selection already belongs
  to that category.
- `web/src/pages/blastSubmit/QuerySection.tsx` — `SectionHeader step={2}` →
  `step={3}`, gated textarea + upload + Load example + Reverse complement +
  Deduplicate + Clear behind `dbSelected`, disabled query subrange inputs until
  a DB is picked, added a one-line notice when no DB is picked, `loadExample`
  now respects `example.blastProgram` and resets stale subranges, and
  `QueryExampleDialog` filters by `selectedDbName` with a count subtitle.
- `web/src/pages/BlastSubmit.tsx` — swapped the JSX render order so
  `DatabaseSection` sits at `sectionRefs[2]` and `QuerySection` at
  `sectionRefs[3]`.

## Validation evidence

- `npx vitest run src/pages/blastSubmit/` — **10 files, 134 tests passed**
  (7 assertions in `queryExamples.test.ts`, including selected-DB-only filter
  coverage, minimum-five-per-DB coverage, plus database category auto-selection
  coverage).
- `npm run build` — clean, no TS / ESLint errors (only the pre-existing
  >500 kB chunk warning on `blast-submit-*.js`).
- Tier 2a host-mode loop only; no Container App redeploy required per charter
  §13 (frontend-only change, no sidecar layout / `terminal/Dockerfile*` /
  Bicep touched).

## Sources

- NCBI Entrez E-utilities (`efetch.fcgi`): `NR_024570.1`, `NR_102783.2`,
  `NR_037007.2`, `NR_117678.1`, `NR_044826.2`, `NR_111007.1`,
  `XR_002086439.1`, `PZ411633.1`, `PZ094085.1`, `PZ394474.1`.
- UniProtKB REST (`/uniprotkb/{accession}.fasta`): `P01308`, `P04637`, `P0DTC2`,
  `P68871`, `P99999`.
- RCSB PDB (`/fasta/entry/{id}`): `1EHZ`, `1GID`, `2GDI`, `3D2V`, `5SWE`
  (RNA `U` bases converted to DNA `T` letters where needed).
