---
title: Update indicator deep-links to the Settings → Updates section
description: Clicking the topbar "update available" dot now opens Settings directly on the Updates section instead of the default Appearance tab, so the update information is no longer hidden behind a manual tab switch.
tags:
  - ui
  - user-guide
---

# Update indicator deep-links to Settings → Updates (2026-06-07)

## Motivation

The topbar settings gear shows an "update available" dot
(`useUpgradeAvailability().attention`) when a newer release/commit exists. The
dot was correct, but clicking it called `settingsPanel.open()` with no
argument, so the Settings dialog always opened on its **default first section,
"Appearance"**. The actual update information (latest version, "What's new"
compare link, "Update now") lives in the **"Updates"** section, which the user
had to find and click manually. The reported symptom — "there's an update dot,
but clicking it shows no information" — was exactly this: the dialog opened on
Appearance and the update details looked missing.

(Confirmed live: the indicator opened Settings on Appearance; the Updates
section did contain `new commit 66005b6` + a valid GitHub compare link, just one
manual click away.)

## User-facing change

Clicking the update indicator now opens Settings **directly on the Updates
section** so the latest version, the "What's new" changes link, and the "Update
now" action are visible immediately. When no update is available, the gear opens
Settings on its normal default section as before.

## API / IaC diff summary

- `web/src/components/SettingsPanel.tsx` — exports `SettingsSectionId`; the
  panel accepts an optional `initialSection` prop and focuses it each time it
  opens (the panel stays mounted, so an effect re-applies the requested section
  on every open).
- `web/src/hooks/useSettingsPanel.tsx` — `open()` now takes an optional
  `section?: SettingsSectionId`, threaded to the panel as `initialSection`. A
  new pure `normalizeSettingsSection(arg)` guard coerces the argument to a known
  section id or `undefined`, so call sites that wire `open` straight to an
  `onClick` (which would pass a click event) still open on the default section
  rather than an invalid one.
- `web/src/components/Layout.tsx` — the settings gear opens
  `settingsPanel.open("updates")` when `upgrade.attention` is set, otherwise
  `open(undefined)`.
- No backend / IaC change.

## Validation evidence

- `cd web && npx vitest run src/hooks/useSettingsPanel.test.ts` — 3 passed
  (every known section forwarded; a stray click-event / unknown / non-string
  argument is dropped to `undefined`).
- `cd web && npx vitest run` — 729 passed (78 files), no regression.
- `cd web && npx tsc --noEmit` and `npx eslint` on the three files — clean.
- `cd web && npm run build` — clean.
- Live repro before the fix: clicking the topbar "Update available" indicator
  opened Settings on "Appearance"; the "Updates" section (one manual click
  away) showed `new commit 66005b6` + `compare/6517596...66005b6` link.
