---
title: API Reference Core section placement and stopped-cluster visibility
description: Move the always-on Core control-plane section into the two-column layout above the spec-derived System group, keep its teal accent, and keep it visible while the cluster is stopped.
tags:
  - ui
  - blast
---

# API Reference Core section placement and stopped-cluster visibility

## Motivation

The always-on **Core** control-plane section (which now carries
`ensure-running` plus the cluster-independent `GET /api/aks/openapi/databases`
and `GET /api/aks/openapi/databases/{db_name}` endpoints) was rendered as a
standalone teal box pinned at the **top** of the API Reference page, visually
detached from the spec-derived endpoint groups (System, Cluster, Databases,
Jobs) that render in the two-column layout below it. That made the page read
inconsistently — the one section that must stay reachable while the cluster is
stopped looked like a different widget rather than a peer of the other groups.

## User-facing change

- The Core section is no longer a separate box at the top of the page. It is now
  the **first section in the right column of the two-column layout, directly
  above the spec-derived `System` group**, so it reads consistently with the
  other endpoint groups (same card flow, same column).
- Its **teal accent is preserved** — the teal icon tile, the `CONTROL PLANE`
  badge, and the "different host" banner still visually distinguish it from the
  blue-accented spec groups. Only the outer teal box wrapper was removed so the
  section shares the plain-section styling of the other groups.
- The Core section **remains visible while the cluster is stopped** (or while
  the live OpenAPI spec is still loading / failed). In those states the
  spec-derived groups and the sidebar are hidden, but the layout collapses to a
  single column that still renders the Core section — because its
  `ensure-running` endpoint is exactly how a stopped cluster is woken.

## API / IaC diff summary

- `web/src/pages/ApiReference.tsx` — removed the standalone top-of-page Core
  render; moved `CoreApiSection` into the two-column layout as the first
  right-column section. Added a `showApiGroups` gate
  (`spec && grouped.length > 0 && !clusterStopped`) that controls the sidebar +
  spec groups while the layout container itself renders whenever a cluster
  context is known (`enabled && clusterName`), so Core survives a stopped
  cluster. The container switches to a single-column flex layout when the spec
  groups are hidden.
- `web/src/pages/apiReference/CoreApiSection.tsx` — removed the outer
  `border` / `background` / `borderRadius` / `padding` box so the section
  matches `TagSection`; teal is retained on the icon tile, badge, and host
  banner. Bumped the endpoint list bottom margin to 24 px to match `TagSection`.
- No backend / IaC change.

## Validation evidence

- `cd web && npm run build` — built successfully.
- `cd web && npx vitest run src/pages/apiReference` — 42 passed.
- `cd web && npx eslint src/pages/ApiReference.tsx src/pages/apiReference/CoreApiSection.tsx`
  — clean.
- Visual: the live deployment still runs the previous build; the placement
  change should be confirmed in the browser after the next frontend deploy.
