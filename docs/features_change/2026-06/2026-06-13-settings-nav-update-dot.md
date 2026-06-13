---
title: Settings left-nav shows an attention dot on the Updates section
description: The topbar gear shows an "update available" dot, but once Settings
  opened nothing indicated which section needed action. The Updates left-nav
  entry now mirrors the gear dot so the user can see where to go.
tags:
  - ui
  - user-guide
---

# Settings nav attention dot on Updates (2026-06-13)

## Motivation

The topbar settings gear renders a calm amber "attention" dot
(`useUpgradeAvailability().attention`) when an update is available, an upgrade is
in progress, or the last run failed / rolled back. Clicking it deep-links to the
**Updates** section. But once the Settings panel was open, the left-nav rendered
every section identically — there was no in-panel cue telling the user *which*
section the gear dot was pointing at. The reported symptom: "the gear has a dot,
but opening Settings shows no dot on the item that needs action."

## User-facing change

- The Settings left-nav **Updates** entry now shows the same small amber dot
  (aligned to the right edge of the row) whenever `upgrade.attention` is set —
  i.e. exactly when the topbar gear dot is lit. It clears automatically once the
  control plane is up to date / the upgrade settles.
- No other section is affected; the dot is scoped to `updates`.

## API / IaC diff summary

- Frontend only. No backend / IaC change.
- `web/src/components/SettingsPanel.tsx`:
  - `SettingsPanel` now reads `attention` from the shared
    `useUpgradeAvailability()` hook (already imported and used by
    `UpdatesSection`; the hook is broadcast-synced across consumers).
  - The nav `.map` renders a 7px amber dot (`var(--warning)`) on the `updates`
    row when `attention` is true, with `aria-label` / `title` "An update is
    available".

## Validation evidence

- `cd web && npm run build` — green (built in ~4s).
- `cd web && npm test -- --run src/hooks/useSettingsPanel.test.ts src/api/upgrade.test.ts`
  — 23 passed.
- No SettingsPanel render test exists; the hook usage is identical to the
  existing `UpdatesSection` consumer, so no test fixtures changed.
