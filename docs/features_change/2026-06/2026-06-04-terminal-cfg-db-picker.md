---
title: Terminal Config Builder — live database picker and simplified form
description: The terminal Config Builder now lists actually-available databases and collapses environment overrides behind an Advanced disclosure.
tags:
  - terminal
  - ui
---

# Terminal Config Builder — live database picker and simplified form

## Motivation

The terminal Config Builder exposed all eleven `elb-cfg` fields as free-text
inputs in a single grid. Users had to memorise a magic database path
(`blast-db/<name>/<name>`) and were faced with seven environment-derived fields
(machine type, region, resource group, storage account, ACR name, output path)
that the platform fills in by default. The form "looked too complex" and was
not immediately usable.

## User-facing change

* **Database is now a live picker.** The free-text database field is replaced by
  a dropdown populated from `GET /api/blast/databases` for the saved workspace
  (subscription + storage account + resource group). Only ready/downloaded
  databases are listed (mirrors the Submit page's `isBlastDbReady` filter), and
  selecting one writes the correct `container/prefix/name` path via the shared
  `buildDatabasePath` helper.
  * Loading, empty, error, and "no workspace configured" states each render a
    helpful hint and fall back to a manual path input.
  * An "Enter a custom path…" option reveals the free-text input for custom
    `makeblastdb` builds or ad-hoc uploads, so nothing is lost.
* **Inputs minimised.** Only the fields a researcher actually fills in stay
  visible: Program, Database, Queries, Results. The seven environment overrides
  move into a collapsed **Advanced (environment overrides)** disclosure that is
  closed by default.
* **Reset** now also closes the Advanced section and clears the manual-path
  mode.

## API / IaC diff summary

* No backend, API, or IaC changes. Frontend-only; reuses the existing
  `blastApi.listDatabases` client and `/api/blast/databases` route.
* The `ElbCfgFormFields` model and `buildElbCfgCommand` contract are unchanged,
  so `terminalCockpitModel.test.ts` (30 tests) stays green.

## Files changed

* `web/src/pages/terminal/TerminalCfgForm.tsx` — DB picker via TanStack Query,
  essential/advanced field split, Advanced disclosure.
* `web/src/theme/glass.css` — styles for the Database field label and the
  Advanced toggle button.

## Validation evidence

* `cd web && npm run build` — succeeds (`✓ built in 7.67s`).
* `cd web && npm test -- --run` — 617/617 passing (69 files), including
  `terminalCockpitModel.test.ts` and `usePreferences.fixtureContract.test.ts`.
* `npx eslint src/pages/terminal/TerminalCfgForm.tsx` — clean.
* Diff audit — only `TerminalCfgForm.tsx` and `glass.css` changed for this work.
