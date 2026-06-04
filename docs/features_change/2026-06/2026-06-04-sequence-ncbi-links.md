---
title: Fix invalid NCBI deep-links on the sequence detail and BLAST taxonomy views
description: Switch retired NCBI taxonomy browser links to the modern Datasets browser and make organism-scoped Entrez searches use the canonical taxid form so they stop failing.
tags:
  - blast
  - ui
---

# Fix invalid NCBI deep-links (sequence detail + BLAST taxonomy)

## Motivation

On the accession / sequence detail screen (`/sequence/<ACCESSION>`) reached by
clicking an accession in BLAST results, several "Related NCBI resources" buttons
led to invalid or failing NCBI pages. Two root causes, confirmed against live
NCBI pages:

1. **Retired Taxonomy browser.** The Taxonomy links pointed at the legacy
   `https://www.ncbi.nlm.nih.gov/Taxonomy/Browser/wwwtax.cgi?id=<taxid>` page.
   NCBI is officially retiring that page (Fall 2026); it already renders a
   deprecation banner and points users to the modern Datasets browser
   `https://www.ncbi.nlm.nih.gov/datasets/taxonomy/<taxid>/`.
2. **Fragile organism Entrez search.** The Nucleotide and Gene-symbol searches
   built terms like `Severe acute respiratory syndrome coronavirus 2[orgn]`.
   With an unquoted multi-word organism name, Entrez binds the `[orgn]` field
   tag only to the trailing token (`2[orgn]`) and free-text-ANDs the rest, which
   produces "Search failed!" or wrong results. The canonical, robust form is
   `txid<N>[Organism:exp]` when a taxid is known — verified to be exactly what
   NCBI's own Datasets taxonomy page emits for its "All nucleotide sequences"
   link (`nuccore/?term=txid2697049[organism:exp]`).

The same two defect classes also appeared in the BLAST-results taxonomy panel
(`pages/blastResults/analytics/helpers.ts`) and the taxonomy detail modal
(`components/taxonomy/TaxonomyDetailModal.tsx`), so they were fixed together.

## User-facing change

- Taxonomy buttons now open the modern NCBI Datasets taxonomy browser instead of
  the soon-to-be-retired legacy page (sequence detail "Related NCBI resources",
  the `taxon` db_xref feature links, the BLAST results taxonomy panel rows/tree,
  and the taxonomy detail modal).
- The Nucleotide "records" search and the Gene-symbol search are now scoped with
  `txid<N>[Organism:exp]` when a taxid is resolved (upgraded confidence from
  "search" to "exact" for the Nucleotide link), falling back to a quoted
  `"<organism>"[Organism]` phrase when only a name is available. This stops the
  "Search failed!" outcome from the unquoted multi-word `[orgn]` form.
- BioProject / BioSample / GeneID record links were already correct (verified
  live) and are unchanged.

## API / IaC diff

Frontend only. No backend, API contract, or infrastructure change.

- New `web/src/pages/sequence/ncbiLinks.ts` — `ncbiTaxonomyUrl`,
  `ncbiNucleotideByOrganismUrl`, `ncbiOrganismClause`.
- New `web/src/pages/sequence/ncbiLinks.test.ts` — 9 cases.
- `web/src/pages/sequence/SequenceDetail.tsx` — use the helpers; remove the
  retired `NCBI_TAXONOMY_BASE` / local `NCBI_NUCCORE_SEARCH_BASE` constants.
- `web/src/pages/blastResults/analytics/helpers.ts` and
  `web/src/components/taxonomy/TaxonomyDetailModal.tsx` — modern Datasets URL.

## Validation evidence

- Live NCBI verification:
  - `https://www.ncbi.nlm.nih.gov/datasets/taxonomy/2697049/` → full record
    renders; its own "All nucleotide sequences" link is
    `nuccore/?term=txid2697049[organism:exp]` (confirms both the modern taxonomy
    URL and the taxid-scoped nucleotide form).
  - The legacy `wwwtax.cgi?id=2697049` page renders NCBI's retirement banner.
- `cd web && npx vitest run src/pages/sequence/ncbiLinks.test.ts` → 9 passed.
- `cd web && npx vitest run` → 604 passed (69 files).
- `cd web && npm run build` → green.
- `npx eslint` on all changed files → clean.
