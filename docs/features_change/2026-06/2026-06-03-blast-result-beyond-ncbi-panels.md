---
title: BLAST result panels beyond NCBI Web BLAST
description: Five differentiated result views — triage scatter, taxonomic dereplication, subject coordinate map, inline hit evidence, and a result passport — layered on top of the existing NCBI-parity tabs.
tags:
  - blast
  - ui
---

# BLAST result panels beyond NCBI Web BLAST

## Motivation

The BLAST result screen already mirrors NCBI Web BLAST's Descriptions /
Graphic Summary / Alignments / Taxonomy tabs. NCBI parity is table stakes;
it does not help a researcher *triage* a large hit list, *judge* whether a
score is trustworthy, or *cite* the run reproducibly. These five panels add
value NCBI's UI does not offer, all derived from data already on the page
(no new backend round-trips).

## User-facing change

All five views are read-only, additive, and reuse the existing analytics
state (so they respect the active result filters):

1. **Coverage × Identity triage scatter** (Graphic Summary tab) — every hit
   plotted by query coverage (x) and percent identity (y), point area scaled
   by bit score and colour by review status. Threshold guide lines split the
   plot into ortholog / partial / divergent / marginal quadrants with counts.
   Clicking a point deep-links to that hit in the Alignments tab.
2. **Taxonomic dereplication** (Descriptions tab) — collapses the hit list to
   one representative per species or genus (toggle), folding redundant
   near-identical strains. Each taxon row shows best-hit identity / E-value /
   bit score, expands to its members, and deep-links the best hit.
3. **Subject coordinate map** (Graphic Summary tab) — plots each subject's
   HSPs on the subject axis (1..length) rather than the query axis, surfacing
   multi-HSP tiling, strand flips, and coordinate inversions
   (rearrangements / duplications). Flagged subjects sort to the top. The
   panel only renders when at least one subject carries multiple HSPs, so the
   common single-HSP case is not duplicated from the query-centric ruler.
4. **Inline hit evidence** (Alignments tab) — each pairwise card gains a
   collapsible plain-language read of *why* the hit scored as it did:
   E-value confidence verdict, database-independent bit-score note, and a
   query-coverage interpretation.
5. **Result passport** (all result analytics tabs) — a one-glance provenance
   card with an NCBI-parity badge (precise / drift / approximate), the pinned
   effective search space, and an auto-generated, copy-pasteable Methods
   paragraph for manuscripts.

## Code summary

Frontend-only; no API or IaC changes.

- New pure, DOM-free derivation helpers + unit tests:
  `web/src/pages/blastResults/analytics/derived.ts` (+ `derived.test.ts`,
  25 tests).
- New components:
  `TriageScatterPanel.tsx`, `TaxonRollupPanel.tsx`, `SubjectMapPanel.tsx`,
  `ResultPassportCard.tsx`.
- Wiring:
  - `GraphicSummaryPanel.tsx` — mounts the triage scatter and subject map
    (deep-link via existing `handleBarActivate`).
  - `DescriptionsTabBody.tsx` — mounts the dereplication panel (deep-link via
    existing `handleSubjectDrilldown`).
  - `AlignmentViewer.tsx` — adds the inline `HitEvidence` block.
  - `BlastResults.tsx` — mounts the result passport on result analytics tabs.

## Validation evidence

- `cd web && npm run build` — green (typecheck + bundle).
- `cd web && npm test -- --run` — 63 files, 504 tests passing.
- `npx vitest run src/pages/blastResults/analytics/derived.test.ts` — 25
  passing.
- `npx eslint <new + modified files>` — no findings.
