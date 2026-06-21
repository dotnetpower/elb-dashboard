---
title: Incremental row windowing for the Descriptions hits table
description: The Descriptions table now paints an initial batch of rows and mounts more as you scroll, keeping large hit sets responsive.
tags:
  - blast
  - ui
---

# Descriptions table row windowing (#29)

## Motivation

The Descriptions (hits) table rendered every row up front. A search with a high
`max_target_seqs` or a full-DB scan can return many hundreds of rows, each a
wide multi-cell row — a heavy first paint and sluggish scroll.

## User-facing change

- The table now paints an initial **60 rows** and mounts **60 more** each time a
  sentinel below the table scrolls into view (same proven pattern as the
  Alignments tab). A `Showing N of M hits — scroll for more` line gives context.
- **Sorting, filtering and selection are unchanged**: they operate on the full
  hit set upstream (`useBlastAnalyticsState`), so windowing only bounds what is
  painted — selecting "all", sorting a column, or filtering still spans every
  hit, not just the visible rows.

## Code change summary

- [web/src/pages/blastResults/analytics/BlastHitsTable.tsx](../../../web/src/pages/blastResults/analytics/BlastHitsTable.tsx):
  added `visibleCount` state (`INITIAL_ROWS` / `ROW_STEP`), an
  `IntersectionObserver` on a sentinel below the table that bumps the window,
  a reset effect keyed on the `hits` array identity, and a "Showing N of M"
  footer. The render maps `visibleHits = hits.slice(0, visibleCount)`.

## Validation evidence

- `cd web && npm run build` → clean.
- `cd web && npm test -- --run BlastHitsTable` → 10 passed (sort/selection logic
  unchanged).
- `npx eslint` on the changed file → clean.
- Live verification by the maintainer on a large-hit-set job after deploy.

No backend / API / IaC changes.
