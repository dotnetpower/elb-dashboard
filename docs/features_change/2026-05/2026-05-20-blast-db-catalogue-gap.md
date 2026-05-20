# BLAST submit: surface the full NCBI standard nucleotide catalogue in the database dropdown

## Motivation
A user comparing our submit page with NCBI Web BLAST asked whether we
could match NCBI's behaviour of listing the full **Standard databases**
reference catalogue inside the dropdown (`core_nt`, `nt`, `refseq_*`,
`wgs`, `est`, `sra`, `tsa`, `tls`, `htgs`, `pat`, `RefSeq_Gene`, `gss`,
`dbsts`, `pdb`, …) rather than only the databases that happen to be in
our `blast-db` storage container.

Today we only show what `/api/blast/databases` returns, so the operator
cannot discover that, e.g., `refseq_rna` is a thing NCBI exposes.

## User-facing change
- The Standard / rRNA·ITS / Genomic+transcript tabs now show the full
  NCBI reference catalogue alongside what is actually downloaded.
- The tab badge reads `"<downloaded> /+<catalogue>"` — e.g. the Standard
  tab on a fresh cluster shows `2 /+11`, meaning the operator has two
  databases in storage and eleven more they could choose to add. The
  `+N` portion is grey so the eye still anchors on the downloaded count.
- The dropdown now uses two `<optgroup>`s: **In storage (ready)** lists
  the immediately submittable databases (unchanged behaviour); **Available
  from NCBI (not downloaded yet)** lists the catalogue gap as disabled
  options so the operator can read the canonical NCBI labels, sizes and
  short descriptions without being able to submit against a database that
  is not yet downloaded.
- A small "Add one from the Dashboard Storage card" hint sits below the
  dropdown whenever the catalogue gap is non-empty, linking to `/` (the
  Storage card already drives the actual download).
- The categorisation regex has been tightened so `refseq_*` databases now
  surface under **Standard databases** (matching NCBI), and the new
  `wgs`/`tsa`/`est`/`refseq_genomes`/`refseq_reference_genomes` entries
  surface under **Genomic + transcript**.

No backend or infra changes — the catalogue is the existing
`DB_CATALOG` in `web/src/components/cards/storageDbCatalog.ts`,
extended with NCBI's standard nucleotide list.

## Diff summary
- `web/src/components/cards/storageDbCatalog.ts`
  - +14 catalogue entries: `refseq_select`, `refseq_rna`,
    `refseq_reference_genomes`, `refseq_genomes`, `wgs`, `est`, `sra`,
    `tsa`, `tls`, `htgs`, `pat`, `RefSeq_Gene`, `gss`, `dbsts`, `pdb`,
    `28S_fungal_sequences`. Sizes are NCBI's 2026 published figures,
    rounded conservatively (they only drive the download UX).
- `web/src/pages/blastSubmit/DatabaseSection.tsx`
  - `databaseCategoryByName(name, source)` extracted so it can classify
    both downloaded `BlastDatabase` records and catalogue strings.
  - `notDownloadedCatalogue(category, downloaded, programType)` filters
    `DB_CATALOG` by category + nucleotide/protein type and drops anything
    already in storage.
  - Per-tab badge now shows downloaded + faded catalogue count, with a
    descriptive `title` tooltip.
  - Dropdown re-rendered with `<optgroup>`s for "in storage" and
    "available from NCBI" (the latter disabled).
  - Helper hint with a `Download` icon links to `/` when the catalogue
    gap is non-empty.
- `web/src/theme/glass.css`
  - `.blast-search-set-tab__hint` (faded `+N` count beside the badge).
  - `.blast-db-add-hint` (dashed accent-tinted helper banner under the
    dropdown).

## Validation
- `cd web && npx tsc --noEmit` → clean.
- `cd web && npx eslint --max-warnings 0 src/pages/blastSubmit/DatabaseSection.tsx src/components/cards/storageDbCatalog.ts`
  → clean.
- `cd web && npm run build` → built in ~8 s.
- `cd web && npx vitest run` → 19 files, 185 tests passed.
- Manual (Playwright + screenshot): Standard tab now shows `2 /+11`, the
  dropdown lists `core_nt` and `elb_compare_tiny` under "In storage" and
  eleven NCBI databases under "Available from NCBI" (each suffixed with
  `— Not downloaded`). Helper hint reads "11 more NCBI nucleotide
  databases are listed above as reference. Add one from the Dashboard
  Storage card to make it submittable."

## Out of scope
- Auto-triggering downloads from the submit page. The Storage card
  already owns the download flow and the catalogue size estimates here
  intentionally route the operator through that flow.
- Protein-side catalogue beyond `pdb` (the NCBI screenshot the user
  referenced was the nucleotide tab).
