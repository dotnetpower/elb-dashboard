---
title: Extract pure helpers and shared atoms out of ClusterBento
description: >-
  ClusterBento's pure submit-volume aggregations and its shared summary atoms
  were moved into dedicated modules (with unit tests for the pure logic),
  trimming the component and improving testability.
tags:
  - ui
  - contributor
---

# Extract pure helpers and shared atoms out of ClusterBento

## Motivation

Issue [#24](https://github.com/dotnetpower/elb-dashboard/issues/24) Priority 2
flags `web/src/components/cards/ClusterBento/ClusterBento.tsx` (1112 lines) as
mixing multiple concerns. The lowest-risk, highest-value slice is to lift the
**pure, side-effect-free logic** and the **shared presentational atoms** out of
the component so they can be unit-tested and reused, without touching the live
bento render tree (which can only be fully validated against a running cluster).

## User-facing change

None. Pure structural refactor — the relocated functions keep identical
signatures and the call sites are unchanged, so the rendered output is
identical.

## What changed

- New [web/src/components/cards/ClusterBento/submitMetrics.ts](../../../web/src/components/cards/ClusterBento/submitMetrics.ts)
  (83 lines) — the pure `submitWindow` + `submitTimeline` aggregations over the
  cluster's BLAST job list (no React, no I/O), now exported with a named
  `SubmitWindow` result type.
- New [web/src/components/cards/ClusterBento/submitMetrics.test.ts](../../../web/src/components/cards/ClusterBento/submitMetrics.test.ts)
  (93 lines) — 7 unit tests covering the 15m/1h/24h windows, active-job
  counting, average-runtime, unparseable timestamps, and per-minute bucketing
  (previously these had **no** direct coverage).
- New [web/src/components/cards/ClusterBento/clusterSummaryHelpers.tsx](../../../web/src/components/cards/ClusterBento/clusterSummaryHelpers.tsx)
  (99 lines) — `SummaryRow`, `emptyNodeSummary`, `topologyNodesLabel`,
  `topologyPoolsLabel`, the summary atoms shared by both the live render and the
  `ClusterReadinessBento` fallback render. Moved here so the two paths reference
  one definition instead of the component holding both.
- [web/src/components/cards/ClusterBento/ClusterBento.tsx](../../../web/src/components/cards/ClusterBento/ClusterBento.tsx)
  (1112 → 949 lines) imports the relocated helpers and drops the now-unused
  `NodeSummary` type import.

## Validation evidence

- `cd web && npm run build` — clean (tsc typecheck + vite bundle).
- `cd web && npx eslint <the 4 files>` — clean.
- `cd web && npx vitest run` — **830 passed** (94 files; +7 new submitMetrics
  tests, no regression).
- Visual smoke: the dashboard root renders cleanly on the local host-mode dev
  server, confirming the ClusterBento module graph imports. Because only pure
  helper definitions + shared atoms moved (identical signatures, unchanged call
  sites), render parity is structurally guaranteed.

## Scope note (deferred — full <600 split)

ClusterBento is still 949 lines: the bulk is the live "Mission Control" bento
render (a 3-column grid of independent data cells). Extracting those cells would
thread 5-10 derived view-model props per cell and risks subtle grid/layout
regressions that **cannot be visually validated locally** (the bento only
renders against a configured, running AKS cluster). Per the #24 acceptance
criteria ("split **or** explicitly deferred"), the cell-level render extraction
is deferred to a follow-up that can be validated against a live cluster. The
`ClusterReadinessBento` fallback render was intentionally left in place for the
same reason (verbatim-move risk for a path that still would not bring the file
under 600).
