---
title: BLAST Results UX batch (copy view link + sortable files table)
description: Added a copy-deep-link button to the result header and made the Files table sortable by name, size and modified time.
tags:
  - blast
  - ui
---

# BLAST Results — UX batch

## Motivation

Continuation of the UI/UX pass on the BLAST Results page. Two gaps remained
after confirming the table sort indicators and empty states already exist:
results couldn't be shared as a link to the exact view, and the result Files
table rendered in API order with no way to sort.

> Verified-already-present (left untouched): Descriptions table sort indicators
> (`SortableHeader` in `BlastHitsTable`), zero-hit empty states
> (`DescriptionsTabBody`, `NoResultFilesPanel`), and Alignments incremental
> windowing. Deferred (need streaming/virtualization or visual tuning):
> per-file download progress bar (#24), Descriptions virtualization (#29),
> per-tab scroll restore (#21), wide-table scroll-shadow cue (#25), taxonomy
> expand/collapse-all (#26), and keyboard row navigation (#30).

## User-facing change

- **#28 Copy link** — the result header action row gains a **Copy link** button
  that copies the current view URL (including the active `?tab=`), so a specific
  result tab can be shared and reopened exactly.
- **#27 Sortable Files table** — the Files tab table headers (File / Size /
  Modified) are now clickable to sort, with a chevron direction indicator and
  `aria-sort`. Default stays API order until a header is clicked; name sorts
  ascending, size / modified default to largest / newest first.

## Code change summary

- [web/src/pages/blastResults/BlastJobHeader.tsx](../../../web/src/pages/blastResults/BlastJobHeader.tsx):
  `handleCopyLink` (copies `window.location.href`) + a `Link2` "Copy link"
  button next to "Copy citation".
- [web/src/pages/blastResults/BlastResultsTable.tsx](../../../web/src/pages/blastResults/BlastResultsTable.tsx):
  `ResultsFileTable` now holds `sortBy` / `sortDir` state and a `useMemo`
  sort; `ResultsHeaderCell` renders a sortable button with `ChevronUp`/
  `ChevronDown` indicators and `aria-sort`.

## Validation evidence

- `cd web && npm run build` → clean.
- `cd web && npm test -- --run blastResults BlastResults` → 145 passed
  (including the existing `BlastJobHeader.test.ts`).
- `npx eslint` on both changed files → clean.

No backend / API / IaC changes.
