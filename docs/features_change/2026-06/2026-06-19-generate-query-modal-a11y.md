---
title: Improve Generate-query modal readability (WCAG AA)
description: The NCBI "Generate query" modal no longer dims the unselected strand toggle with 50% opacity and no longer uses dark-only hardcoded row backgrounds, so its text meets WCAG AA contrast in both themes and selection is conveyed without relying on colour alone.
tags:
  - ui
  - blast
---

# Improve Generate-query modal readability (WCAG AA)

## Motivation

The New Search **Generate query from NCBI** modal
(`SequenceBuilderDialog`) had three accessibility/readability defects:

1. The unselected **strand toggle** (`Plus` / `Minus`) was dimmed with
   `opacity: 0.5`. Opacity dimming drops the effective text contrast below
   WCAG AA and conveys the selected state by colour/brightness alone.
2. The **NCBI search-result rows** and **gene-feature rows** used hardcoded
   `rgba(255,255,255,0.04)` backgrounds (and `rgba(120,170,255,0.12)` for the
   selected row). These are dark-theme-only: on the light theme's solid-white
   panels they render as near-invisible rows, losing the card affordance.

## User-facing change

- The strand toggle now keeps **both** labels at full, AA-compliant contrast:
  the unselected button uses `--text-muted` (≈5.4:1 dark / ≈7:1 light) and the
  selected one is shown with a filled background, focus-coloured border, and
  bold weight — plus `aria-pressed` so screen readers get the state, not just a
  colour cue.
- The search-result and feature rows now use the theme-aware `--bg-tertiary`
  surface, with the selected accession row tinted via
  `color-mix(in srgb, var(--accent) 16%, var(--bg-tertiary))` and a
  `--border-focus` outline, so the rows are clearly visible in both light and
  dark themes. Selected rows also expose `aria-pressed`.

No behaviour change — only styling and ARIA state.

## API / IaC diff summary

Frontend only: `web/src/pages/blastSubmit/SequenceBuilderDialog.tsx`. No backend,
no API, no IaC change.

## Validation evidence

- `npx eslint src/pages/blastSubmit/SequenceBuilderDialog.tsx` — clean.
- `npx vitest run src/pages/blastSubmit/SequenceBuilderDialog.test.ts` — 14 passed
  (pure helpers unaffected).
- `npm run build` — succeeds.
- Contrast: unselected toggle `--text-muted` on `--bg-primary`/white meets AA
  (≥4.5:1) in both themes; previously the 50%-opacity label fell well below AA.
