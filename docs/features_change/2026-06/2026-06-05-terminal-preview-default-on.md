---
title: Terminal preview enabled by default
description: The Settings → Preview "Terminal preview" toggle now defaults to on
  so the browser terminal route, navigation entry, dashboard card, and keyboard
  shortcut are available out of the box.
tags:
  - ui
  - terminal
---

# Terminal preview enabled by default

## Motivation
The browser terminal is a core surface of the control plane, but its Settings →
Preview opt-in (`previewTerminalEnabled`) shipped disabled by default, so new
users did not see the terminal route, nav entry, dashboard card, or keyboard
shortcut until they manually flipped the toggle. The request is to make the
terminal available out of the box.

## User-facing change
- The **Settings → Preview → Terminal preview** toggle now defaults to **on**.
- The browser terminal route, navigation entry, dashboard card, and keyboard
  shortcut are visible by default. Operators who prefer to hide it can still
  turn the toggle off; an explicit off choice is persisted and respected.
- Existing users who already toggled the preference keep their stored choice
  (the default only applies when the key is absent from `elb-prefs`).

The runtime feature flag (`VITE_FEATURE_TERMINAL`) already defaulted to enabled,
so this preference flip is the only gate that needed to change for the terminal
to surface by default.

## API / IaC diff summary
- No backend or IaC change. Frontend only — `web/src/hooks/usePreferences.tsx`:
  - `DEFAULT_PREFERENCES.previewTerminalEnabled` flipped `false` → `true`.
  - Updated the field docstring to reflect the new default.

## Validation evidence
- `cd web && npm run build` — succeeds.
- `cd web && npm test -- --run` — 70 files / 633 tests passing.
