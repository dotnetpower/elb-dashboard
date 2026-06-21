---
title: BLAST live-log auto-scroll no longer drops the final tail lines
description: Fixed a rAF-coalescing race in useStickToBottom that left the last streamed log lines below the fold, and added a "jump to latest" pill when auto-follow is paused.
tags:
  - blast
  - ui
---

# BLAST live-log tail follow — fix dropped tail + "jump to latest" pill

## Motivation

On the BLAST **Run details → Execution Steps** card (the same live log shown
while a job is *submitting* / *running*), the auto-scroll *did* follow the
tail, but users reported that **new log lines sometimes piled up at the bottom
of the screen without being scrolled into view** — the viewport stopped one
chunk short of the newest output.

Root cause — a **dropped-request race** in
[useStickToBottom](../../../web/src/hooks/useStickToBottom.ts):

- The live log DOM grows continuously as SSE events arrive. Each growth fires a
  `ResizeObserver` callback that requests a scroll-to-tail.
- Scroll requests were rAF-coalesced with `if (rafRef.current !== null) return`
  — i.e. **any request that arrived while a previous rAF was in flight was
  silently dropped.** While the stream is busy the *next* growth usually catches
  up, so it looked fine. But the **final** line (the one that arrives exactly as
  the previous rAF runs, right before the stream goes quiet) had no successor to
  re-trigger the scroll, so its lines stayed below the fold.

## User-facing change

- The live log now follows the tail reliably **all the way to the last line** —
  no more "logs appearing un-scrolled at the bottom".
- When the user scrolls up to read history (auto-follow pauses), a calm glass
  **"Jump to latest"** pill appears at the bottom-centre. Clicking it re-arms
  following and snaps to the newest output. The pill disappears once you are
  back at the tail. It respects `prefers-reduced-motion` and has a visible
  focus ring.

## Code change summary

- [web/src/hooks/useStickToBottom.ts](../../../web/src/hooks/useStickToBottom.ts):
  - Replaced the drop-on-busy rAF guard with a `pendingScrollRef` flag. A
    request that lands while a rAF is in flight now sets the flag, and the
    in-flight pass **re-asserts** the scroll on its settle frame instead of
    discarding it — the final growth is never lost.
  - Tracked the inner (settle) rAF id so unmount cancels whichever frame is
    pending.
  - Added an optional `onFollowingChange(following)` callback and an imperative
    `{ scrollToTail }` return so a host can render a "jump to latest" affordance
    and re-arm following. Follow transitions are routed through a single
    `markFollowing` helper that only notifies on an actual change.
  - Public pure helpers (`shouldFollow`, `shouldFollowAnchor`,
    `anchorFollowTarget`) are unchanged — existing unit tests stay green.
- [web/src/pages/blastResults/ExecutionStepsCard.tsx](../../../web/src/pages/blastResults/ExecutionStepsCard.tsx):
  tracks follow state via `onFollowingChange` and renders the `Jump to latest`
  pill (with `ArrowDownToLine` icon) when following is paused.
- [web/src/theme/glass.css](../../../web/src/theme/glass.css): added the
  `.blast-jump-latest` glass pill style (fixed bottom-centre, blur surface,
  hover/focus states, reduced-motion guard).

## Validation evidence

- `cd web && npm test -- --run useStickToBottom` → 13 passed (pure follow/pause
  + anchor geometry contract unchanged).
- `cd web && npm run build` → TypeScript + Vite build clean (hook return-type
  change compiles across its sole consumer).
- `npx eslint` on both changed `.ts`/`.tsx` files → clean.

No backend / API / IaC changes.
