---
title: Auto-refresh cadence ring on the dashboard chip
description: Wired the existing RefreshRing into the Auto-refresh chip to show an approximate countdown to the next refresh cycle.
tags:
  - ui
  - overview
---

# Auto-refresh cadence ring (#33)

## Motivation

The `RefreshRing` component existed but was unused. The Auto-refresh chip let
users pick a refetch interval but gave no visual sense of the rhythm.

## User-facing change

- The **Auto-refresh** chip now shows a small countdown ring next to the
  interval dropdown, ticking down to the next refresh cycle and resetting when
  the interval changes. The countdown pauses while the tab is hidden.

> Honesty note: dashboard cards refetch on their own TanStack Query timers, so
> the ring reflects the *configured cadence*, not a guarantee tied to a single
> query's refetch. It is a rhythm indicator, which is why the title stays
> generic.

## Code change summary

- [web/src/hooks/useAutoRefresh.tsx](../../../web/src/hooks/useAutoRefresh.tsx):
  added a `secondsToRefresh` countdown (1s ticker, resets on interval change,
  pauses when `document.hidden`) to the context.
- [web/src/pages/Dashboard/AutoRefreshChip.tsx](../../../web/src/pages/Dashboard/AutoRefreshChip.tsx):
  render `RefreshRing` with `secondsToRefresh` + `total`.

## Validation evidence

- `cd web && npm run build` → clean.
- `npx eslint` on both changed files → clean.

No backend / API / IaC changes.
