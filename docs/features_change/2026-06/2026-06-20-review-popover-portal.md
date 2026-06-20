---
title: Fix review-badge popover clipped by table overflow / sticky header
description: The BLAST results review-badge popover now renders in a portal with fixed positioning so it is no longer clipped by the results table's overflow or covered by the sticky header.
tags:
  - ui
  - blast
---

# Fix review-badge popover clipped by table overflow / sticky header

## Motivation

On the BLAST results **Descriptions** tab, hovering a row's review badge
(`Strong` / `Weak` / `Unknown` …) opened a details popover whose top was covered
by the panel/sticky header above it. The popover was rendered inline as a
`position: absolute` child of the table cell, so it was trapped inside the
results table's stacking + clipping context: the table sits in a
`glass-card { overflow: hidden }` with an inner `overflow-x: auto` scroller, and
`overflow-x: auto` forces the cross-axis to clip too — so the popover was both
**clipped** by the scroller and **painted over** by the sticky selection/header
bar. Bumping `z-index` cannot fix an overflow-clipping problem.

## User-facing change

The review-badge popover now renders in a **portal to `document.body`** with
**fixed positioning** computed from the badge's viewport rect, so it fully
escapes the table's overflow clipping and sticky-header stacking and always
floats above the page. Behaviour is otherwise unchanged:

- Opens on hover / focus / click; closes on Escape, outside-click, or blur.
- A short hover-bridge delay keeps it open while the pointer travels from the
  badge to the (now detached) popover.
- It still flips above the badge when there is not enough room below, and now
  re-anchors on scroll / resize while open so it tracks the badge.
- Horizontal position is clamped to the viewport so it never overflows the edge.

## API / IaC diff summary

Frontend only: `web/src/pages/blastResults/analytics/ReviewBadgePopover.tsx`
(inline absolute popover → `createPortal` + `position: fixed` with rect-based
coordinates, hover-bridge close timer, scroll/resize reposition, outside-click
now also ignores clicks inside the portaled popover). No CSS change; the
existing `.review-popover` z-index suffices once the element is a body child. No
backend / API / IaC changes.

## Validation evidence

- `npm run build` — succeeds; `npx eslint` on the file — clean.
- `npx vitest run src/pages/blastResults/analytics` — 99 passed (incl.
  `BlastHitsTable` which mounts the badge, and `reviewBadgeMeta`).
- The fix is the standard portal-escape pattern for popovers inside a scrollable
  table; a completed-job results page was not reproducible on the empty local
  stack, and the reported screen is a remote deployment, so verification is by
  static build/lint/test plus the established pattern.
