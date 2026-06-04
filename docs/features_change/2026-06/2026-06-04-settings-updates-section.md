---
title: Move upgrade discovery from header badge to Settings → Updates
description: The header upgrade badge is removed and replaced by a Settings
  "Updates" section that polls upgrade status passively and adds an explicit
  "Check now" button, plus a link to the full self-upgrade page.
tags:
  - ui
  - release
---

# Settings → Updates section

## Motivation
The self-upgrade indicator lived as a small badge next to the version stamp in
the app header. It was easy to miss, could not be triggered on demand, and put
upgrade discovery in a high-traffic chrome surface. Operators asked for a calmer
home: surface availability in Settings, allow an explicit check, and still show
an update passively when one exists.

## User-facing change
- The header no longer renders the upgrade badge (`UpgradeBadge` component
  removed).
- Settings gains an **Updates** section (left-nav entry with an
  `ArrowUpCircle` icon) showing:
  - **Current version** (running version + short commit).
  - **Latest available** — a badge that reads `Up to date`, `vX.Y.Z` when an
    update is available, `Not configured` when no git remote is set, or
    `Loading…` on first paint. This refreshes automatically every 60s (gated by
    tab visibility), so an available update appears **without** any click.
  - **Upgrade in progress** row when a self-upgrade is mid-flight (phase
    progress %).
  - **Check for updates** → a `Check now` button that forces a
    `/api/upgrade/check`. It absorbs the backend 429 throttle by showing a
    "try again shortly" hint instead of an error.
  - **Manage upgrade** → a link to the full `/upgrade` page (closes the Settings
    panel on navigation).
- When no upgrade remote is configured, an inert info line explains that new
  releases will not surface until an operator sets `UPGRADE_GIT_REMOTE`.

## API / IaC diff summary
- No backend or IaC change. The section reuses the existing
  `upgradeApi.status()` (GET `/api/upgrade/status`) and `upgradeApi.check()`
  (POST `/api/upgrade/check`) typed clients and the `isUpgradeAvailable` /
  `statePhase` helpers in `web/src/api/upgrade.ts`.
- Frontend only:
  - `web/src/components/Layout.tsx` — removed the `UpgradeBadge` import and its
    render in the header.
  - `web/src/components/UpgradeBadge.tsx` — deleted (its only consumer was the
    header).
  - `web/src/components/SettingsPanel.tsx` — added the `updates` section id,
    nav entry, and the new `UpdatesSection` component.

## Validation evidence
- `cd web && npm run build` — built in 7.77s, no type errors.
- `cd web && npm test -- --run` — 69 files, 616 tests passing.
- `npx eslint src/components/SettingsPanel.tsx src/components/Layout.tsx` — 0
  errors (1 pre-existing unrelated warning at line 2144).
- `grep -rn UpgradeBadge web/src` — no remaining references after deletion.
