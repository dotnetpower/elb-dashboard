---
title: BLAST run live log auto-scroll follows smoothly line-by-line
description: Replaced the coarse debounced scroll trigger with a ResizeObserver so the BLAST Execution Steps live log follows the tail continuously like GitHub Actions.
tags:
  - blast
  - ui
---

# BLAST run live log — smooth line-by-line auto-scroll

## Motivation

On the BLAST results "Execution Steps" card, the live log auto-scroll *did*
follow the tail, but in a jerky, multi-second "lurch": the viewport would sit
still for a few seconds and then jump down a large chunk. GitHub Actions (and
our own [BuildLogViewer](../../../web/src/components/BuildLogViewer.tsx) on the
upgrade page) feel smooth by comparison.

Root cause — a **trigger-granularity mismatch** in
[useStickToBottom](../../../web/src/hooks/useStickToBottom.ts):

- The live log DOM grows **continuously** as SSE events arrive
  (`useBlastJobLogStream` appends each line).
- The scroll trigger (`version`) was
  `phase | updated_at | submitting.log_line_count`, all derived from the
  backend's **debounced** job-state writes (~seconds apart).

So the content grew every line but the scroll only fired every few seconds —
producing the lurch.

## User-facing change

The Execution Steps live log now follows the tail **continuously, line by
line**, matching the GitHub-Actions feel:

- A `ResizeObserver` on `document.body` scrolls to the bottom on **every**
  body-height growth (each appended line), instead of only on the coarse
  debounced token.
- Manual scroll control is unchanged and now more robust: scrolling up pauses
  auto-follow; returning to the bottom re-arms it. The hook ignores the single
  self-induced scroll event from its own programmatic scroll so it never
  mis-detects auto-scroll as a manual scroll-away.
- Rapid growth bursts are coalesced into one `requestAnimationFrame` scroll to
  avoid layout thrash.

## Code change summary

- [web/src/hooks/useStickToBottom.ts](../../../web/src/hooks/useStickToBottom.ts):
  - Added a `ResizeObserver`-driven smooth-follow effect (the primary fix).
  - Extracted the pure `shouldFollow(scrollTop, viewportHeight, documentHeight, threshold?)`
    decision so the user-control contract is unit-testable.
  - Added a self-scroll guard + rAF coalescing.
  - Kept the existing `version` effect for initial force-scroll (landing on a
    completed job's tail) and phase-transition cues. Public signature
    (`{ version, enabled }`) is unchanged — the sole consumer
    [ExecutionStepsCard](../../../web/src/pages/blastResults/ExecutionStepsCard.tsx)
    needs no change.
- [web/src/hooks/useStickToBottom.test.ts](../../../web/src/hooks/useStickToBottom.test.ts):
  new unit tests for `shouldFollow` (at-bottom, within-threshold, scrolled-up,
  custom threshold, short content).

No backend / API / IaC changes.

## Validation evidence

- `cd web && npm test -- --run useStickToBottom` → 5 passed.
- `cd web && npm test -- --run` → 536 passed (full suite, no regressions).
- `cd web && npm run build` → built successfully (type-check clean).
