---
title: Live Wall tiles get an expandable, filterable full-log view
description: Live Wall sidecar tiles only showed six clipped log lines and the
  expand / inspector buttons were disabled, so an operator could see a "5 ERR"
  count but not the errors. Tiles now open a scrollable, level-filterable full
  log modal, and the ERR / WARN pills are clickable shortcuts into it.
tags:
  - ui
  - operate
---

# Live Wall expandable log view (2026-06-13)

## Motivation

Each Live Wall sidecar tile rendered only the last six log lines, and every line
was clipped to a single row (`white-space: nowrap; text-overflow: ellipsis`), so
long messages were unreadable. The tile's **Expand** (`Maximize2`) button and the
footer **Inspector** button were both hard-`disabled` ("coming soon" / "Phase
2"), and the **`N ERR` / `N WARN`** pills were non-interactive. The reported
symptom: the `worker` tile shows "5 ERR" but there was no way to see what those
five errors actually were.

## User-facing change

- The tile **Expand** button is now enabled and opens a full-log modal.
- The **`N ERR`** / **`N WARN`** pills are now buttons — clicking one opens the
  modal pre-filtered to that level, so "5 ERR" → the five error lines directly.
- The footer button is enabled (relabelled **Full logs**) and opens the same
  modal.
- The modal:
  - Backfills a larger recent tail (`GET /api/monitor/logs/{c}/recent?tail=500`)
    merged with the tile's live SSE buffer, de-duped and time-sorted, and keeps
    updating live while open.
  - Wraps long messages (`white-space: pre-wrap; word-break: break-word`) in a
    scrollable pane instead of clipping them.
  - Offers level chips (**All / ERR / WARN** with counts) and a regex text
    filter, a **Copy** action for the visible lines, and closes on Escape /
    backdrop click.
  - Falls back to the live buffer alone (with a note) when the `recent` route is
    unavailable (older backend), and never opens its own SSE connection — the
    tile still owns the single stream.

## API / IaC diff summary

- Frontend only. No backend / IaC change. Reuses the existing
  `fetchRecentLogs()` typed client (`GET /api/monitor/logs/{container}/recent`,
  `tail` ≤ 2000) and the live `useSidecarLogs` buffer.
- `web/src/pages/Monitor/SidecarLogModal.tsx` — new modal component.
- `web/src/pages/Monitor/SidecarLiveTile.tsx` — enable expand button, make
  ERR/WARN pills + footer button open the modal, render the modal.
- `web/src/pages/Monitor/LiveWall.css` — modal styles + clickable pill/footer
  button states.

## Validation evidence

- `cd web && npx eslint src/pages/Monitor/SidecarLogModal.tsx src/pages/Monitor/SidecarLiveTile.tsx` — clean.
- `cd web && npm run build` — green.
- `cd web && npm test -- --run` — 817 passed (92 files); no Live Wall tests
  existed before, none regressed.
- Backend `GET /api/monitor/logs/{container}/recent` confirmed present in
  `api/routes/monitor/logs.py` (`tail` 1–2000), so the modal backfill works in
  the deployed control plane.
