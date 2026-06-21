---
title: BLAST step-log reader controls + app-wide a11y polish
description: Added line-wrap, font-size and timestamp toggles to the BLAST step-log viewer, plus a global reduced-motion guard and a truncate utility.
tags:
  - blast
  - ui
---

# BLAST step-log reader controls + a11y polish

## Motivation

Follow-up UI/UX pass after the live-log tail-follow fix. Operators reading long
`elastic-blast` step logs wanted reader ergonomics (wrap off for wide tables,
bigger text, hide the timestamp column), and the app lacked a single broad
reduced-motion safety net.

## User-facing change

- **Step-log viewer** (Run details → Execution Steps, and every expanded step):
  the controls row now carries three reader toggles next to the existing
  filter/search:
  - **Wrap** — soft-wrap long lines (default) or switch to horizontal scroll
    so wide `outfmt 7` tables stay aligned.
  - **Timestamps** — show/hide the per-line timestamp column to reclaim width.
  - **A− / A+** — step the log font size through four sizes (10.5–14 px).
  All three are local to each step block, keyboard-reachable, and have
  `aria-pressed` / `sr-only` labels.
- **Reduced motion** — a global `prefers-reduced-motion: reduce` guard now
  collapses every animation/transition to near-instant app-wide (motion is
  removed, never function).
- **`.truncate`** utility class for single-line ellipsis with a `title`
  fallback, available for future card/label polish.

## Code change summary

- [web/src/components/BlastStepTimeline/StepLogBlock.tsx](../../../web/src/components/BlastStepTimeline/StepLogBlock.tsx):
  added `wrap` / `fontStep` / `showTs` local state, a `--step-log-fs` CSS var on
  the root, `data-wrap` / `data-ts` attributes, and the three reader-control
  buttons in the controls row.
- [web/src/theme/glass.css](../../../web/src/theme/glass.css): drove
  `.step-log-line` font-size from `--step-log-fs`; added `data-wrap="off"` /
  `data-ts="off"` rules, `.step-log-view` / `.step-log-fontsize` /
  `.step-log-chip--icon` styles, the global reduced-motion guard, and the
  `.truncate` utility.

## Validation evidence

- `cd web && npm run build` → clean.
- `cd web && npm test -- --run StepLog parseStepLog BlastStepTimeline` → 38
  passed (no regression in the log parse / render / section contracts).
- `npx eslint` on the changed `.tsx` → clean.

No backend / API / IaC changes.
