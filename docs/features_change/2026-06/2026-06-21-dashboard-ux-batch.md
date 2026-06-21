---
title: Dashboard UX batch (toast stack cap + tabular-nums utility)
description: Capped the toast stack to the most recent four and added a shared tabular-nums utility class.
tags:
  - ui
  - overview
---

# Dashboard — UX batch

## Motivation

Continuation of the UI/UX pass on the Dashboard / Monitor surface. Most
candidates were already implemented in this mature area, so this batch lands
only the two genuine gaps and leaves the rest untouched.

> Verified-already-present (left untouched): unified card skeletons
> (`RowSkeleton` + `.skeleton`, tokens just fixed 2026-06-21), shared degraded
> tone (`DegradedNotice`), "n ago" labels (`useRelativeTime`), cluster status
> AA colors (`.gt-*`), empty jobs CTA (`JobsEmptyState`), responsive single-
> column grid (`.dashboard-grid` @768px). Deferred (need a shared refresh clock
> or per-card refetch wiring + visual verification): `RefreshRing` countdown
> integration (#33) and a retry action on `DegradedNotice` cards (#40).

## User-facing change

- **#37 Toast stack cap** — at most four toasts now stack at once; a rapid-fire
  burst (e.g. a failing poll retrying) drops the oldest overflow immediately
  instead of burying the screen.
- **#39 `.tabular-nums` utility** — a shared class for the existing ad-hoc
  `font-variant-numeric: tabular-nums` convention, so future numeric columns
  align without re-declaring the inline style.

## Code change summary

- [web/src/components/Toast.tsx](../../../web/src/components/Toast.tsx):
  `MAX_TOASTS = 4`; the `toast()` reducer slices to the most recent four.
- [web/src/theme/glass.css](../../../web/src/theme/glass.css): `.tabular-nums`
  utility class.

## Validation evidence

- `cd web && npm run build` → clean.
- `npx eslint src/components/Toast.tsx` → clean.

No backend / API / IaC changes.
