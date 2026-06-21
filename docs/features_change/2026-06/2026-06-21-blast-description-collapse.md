---
title: Collapse long BLAST hit descriptions behind a "more" toggle
description: Long subject Descriptions in the BLAST Descriptions table are clamped to 100 characters with a "more"/"less" toggle so a 200+ character title no longer blows up the row height.
tags:
  - user-guide
  - blast
  - ui
---

# Collapse long BLAST hit descriptions behind a "more" toggle

## Motivation
On the BLAST results **Descriptions** tab, the per-hit subject Description
(`stitle`) can range from a few words to 200+ characters (multi-organism /
"PREDICTED:" records). The long ones wrapped over several lines and blew up the
row height, making the table hard to scan.

## User-facing change
The Description column now clamps a title longer than 100 characters to its first
100 characters followed by an ellipsis and an inline **more** button. Clicking
**more** expands the full title in place; **less** collapses it again. Short
titles render verbatim with no button, and an empty title still shows `—`.

## Implementation
* [web/src/pages/blastResults/analytics/BlastHitsTable.tsx](../../../web/src/pages/blastResults/analytics/BlastHitsTable.tsx)
  — added a pure, exported `clampDescription(text, threshold)` helper (empty /
  long / preview decision) and a local `DescriptionCell` component that uses it
  with a `useState` expand toggle. The Description `<td>` now renders
  `<DescriptionCell text={hit.stitle || ""} />`.

## Validation
* `npx eslint` clean on the changed files.
* `npx vitest run BlastHitsTable.test.ts` → 10 passed (4 new `clampDescription`
  cases: empty/whitespace, short verbatim, long clamp + ellipsis, exact-threshold
  no-clamp).
* `npm run build` clean.
