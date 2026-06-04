---
title: Terminal moved behind a Settings → Preview opt-in
description: The browser terminal is now an optional surface, hidden by default and revealed only when the user enables it in Settings → Preview.
tags:
  - ui
  - terminal
---

# Terminal moved behind a Settings → Preview opt-in

## Motivation

The browser terminal is an optional surface, not part of the core BLAST flow.
Previously it was gated only by the build-time `VITE_FEATURE_TERMINAL` runtime
flag, which defaults to `true`, so the terminal route, top-nav entry, dashboard
card, results-page CTA, and `g t` keyboard shortcut were always visible. We want
the terminal to be off by default and surfaced only when a user explicitly opts
in — consistent with the other Preview features (Custom DB, Lab Tools, Live
Wall).

## User-facing change

- A new **Terminal** toggle appears under **Settings → Preview**, off by default.
- With the toggle off, the terminal is fully hidden: no `/terminal` route, no
  navigation entry, no dashboard Terminal card, no `g t` shortcut, and the
  "no result files" panel on the results page no longer offers the terminal CTA.
- Turning the toggle on immediately reveals all of those surfaces (stored in this
  browser only, like the other Preview opt-ins).
- The build-time `VITE_FEATURE_TERMINAL=false` flag still hard-disables the
  feature even if the toggle is on, matching the Custom DB / Lab Tools pattern.

## Terminal Config Builder usability

Alongside the gating change, the terminal's own **Config Builder** form was made
less error-prone so the (now opt-in) feature is more usable:

- **Program** is a dropdown of the five canonical BLAST programs
  (`blastn` / `blastp` / `blastx` / `tblastn` / `tblastx`, mirroring the backend
  `^(blastn|blastp|blastx|tblastn|tblastx)$` contract) with a short
  query→database hint, instead of a free-text input that required memorising the
  exact program name.
- **Nodes** is now a constrained numeric input (`min=1`).
- The generated `elb-cfg` command contract is unchanged — the field values are
  still plain strings flowing through `buildElbCfgCommand`, so the terminal-side
  helper remains the single source of truth for the INI layout.

## Implementation summary

- `web/src/hooks/usePreferences.tsx`: added `terminal` to `PreviewFeature`, a
  `previewTerminalEnabled` preference (default `false`), and wired it into
  `PREVIEW_PREF_KEYS` and `PREVIEW_RUNTIME_FLAGS` (runtime flag `terminal`).
- `web/src/components/SettingsPanel.tsx`: added the **Terminal** toggle row to the
  Preview section.
- Swapped `isFeatureEnabled("terminal")` for `usePreviewFeatureEnabled("terminal")`
  in every consumer: `App.tsx`, `Layout.tsx`, `KeyboardShortcuts.tsx`
  (`shortcuts()` now takes a `terminalEnabled` argument sourced from the hook in
  both the `useKeyboardShortcuts` hook and the `ShortcutsTab` component),
  `pages/Dashboard/DashboardGrid.tsx`,
  `pages/Dashboard/useGettingStartedReadiness.ts`,
  `pages/blastResults/useBlastResultsState.ts`, and
  `pages/blastResults/BlastResultsTable.tsx`.
- `scripts/e2e/fixtures/uiTest.ts`: set `previewTerminalEnabled: true` so the
  ui-mock e2e scenarios that exercise the terminal keep their surfaces mounted
  (also keeps the `usePreferences.fixtureContract` test green).
- `web/src/pages/terminal/TerminalCfgForm.tsx`: declarative `FieldDescriptor`
  now supports `select` / `number` controls; Program renders as a dropdown and
  Nodes as a numeric input. No change to `buildElbCfgCommand` or the model.

## Validation

- `cd web && npm run build` — TypeScript compile + Vite build succeed.
- `cd web && npm test -- --run` — 617/617 tests pass, including
  `usePreferences.fixtureContract.test.ts` and
  `terminalCockpitModel.test.ts` (30 tests).
- `npx eslint` on all changed files — no new errors (one pre-existing unrelated
  warning in `SettingsPanel.tsx`).
