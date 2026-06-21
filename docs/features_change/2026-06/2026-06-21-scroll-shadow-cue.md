---
title: Horizontal scroll-shadow cue for wide tables
description: Added a reusable ScrollShadow component that fades the edges of a horizontally-scrollable container and applied it to the wide taxonomy organism table.
tags:
  - ui
  - blast
---

# Horizontal scroll-shadow cue (#25)

## Motivation

Wide tables (the taxonomy Organism table, the Descriptions hits table) scroll
horizontally with `overflow-x: auto` but gave no visual hint that more columns
existed off-screen.

## User-facing change

- A reusable **`ScrollShadow`** wrapper shows a subtle edge fade on whichever
  side still has hidden content; the fade disappears once that edge is reached.
  Applied to the taxonomy **Organism** table and the **Descriptions** hits table
  (the widest, `minWidth: 1320`) — on the latter only the table is wrapped, not
  the outfmt-gap hint note above it. Respects `prefers-reduced-motion`.

## Code change summary

- [web/src/components/ScrollShadow.tsx](../../../web/src/components/ScrollShadow.tsx):
  new component — owns the overflow container, toggles `--at-start` /
  `--at-end` classes from scroll geometry (scroll listener + `ResizeObserver`).
- [web/src/theme/glass.css](../../../web/src/theme/glass.css): `.scroll-shadow`
  edge-fade styles (gradient from `--bg-secondary` to transparent), opacity
  toggled by the start/end classes, reduced-motion guard.
- [web/src/pages/blastResults/analytics/TaxonomyPanel.tsx](../../../web/src/pages/blastResults/analytics/TaxonomyPanel.tsx):
  wrapped the Organism table in `ScrollShadow`.
- [web/src/pages/blastResults/analytics/BlastHitsTable.tsx](../../../web/src/pages/blastResults/analytics/BlastHitsTable.tsx):
  wrapped the Descriptions table in `ScrollShadow` (outer container loses its
  own `overflow-x`; the hint note stays full-width above it).

## Validation evidence

- `cd web && npm run build` → clean.
- `cd web && npm test -- --run TaxonomyPanel` → 5 passed.
- `npx eslint` on the changed files → clean.

No backend / API / IaC changes.
