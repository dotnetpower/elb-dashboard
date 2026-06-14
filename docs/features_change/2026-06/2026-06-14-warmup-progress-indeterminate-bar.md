---
title: Warmup progress bar no longer looks frozen during pod bootstrap
description: Show an honest indeterminate bar while a warmup is active but has no byte-level percent yet, instead of a frozen empty 0% track.
tags:
  - ui
  - blast
---

# Warmup progress bar — indeterminate state during bootstrap

## Motivation

A user reported that the DB Warmup panel "looks stuck" while a warmup runs.
Live `kubectl` inspection confirmed the warmup itself was **not** stuck — all
node-local warmup Jobs completed normally (16S in ~1-2 min, core_nt in ~8 min,
and the azcopy copy for 16S finished in 6s at 1.2 Gb/s). The problem was purely
visual feedback.

Root cause: `WarmupProgressBar` drove its fill width from
`progress_pct`, which is only a real number once a warmup pod's azcopy emits a
`"%"` log line. During the bootstrap window (image start, `azcopy login`, and
the first seconds of a fast small-DB copy) — and any later gap where no pod
reports a percent — `progress_pct` collapses to `0`. The bar then rendered
`width: 0%` (an empty track) while the only moving element on screen was the
elapsed-seconds counter, so a healthy, actively-copying warmup read as frozen.

## User-facing change

While a database is actively `Loading` but has no determinate byte/percent
signal yet, the per-row progress bar now renders an **indeterminate animated
bar** (a muted accent highlight sliding across the track) that honestly signals
"working, progress unknown". It switches to the normal determinate fill the
instant a real positive percent arrives, and reaches 100% during the
verify → vmtouch tail exactly as before.

No fake/advancing percentage is fabricated: the `"% copied"` label still only
appears for a genuine `0 < pct < 100`. Under `prefers-reduced-motion: reduce`
the indeterminate bar degrades to a static, muted full-width track (no motion).

This is a **frontend-only** change — no backend, image, or warmup-script change,
so no redeploy is required (charter §13).

## API / IaC diff summary

None. No API, schema, or infrastructure change.

## Implementation

- `web/src/components/warmupSection/helpers.ts` — new pure helper
  `isWarmupProgressIndeterminate(warm, pct)`: `true` only while
  `status === "Loading"` and `pct` is non-finite or `<= 0`.
- `web/src/components/WarmupSection.tsx` — `WarmupProgressBar` uses the helper to
  pick the indeterminate vs determinate render branch and adds
  `role="progressbar"` with `aria-valuenow` (omitted when indeterminate).
- `web/src/theme/glass.css` — new `.progress-indeterminate` class +
  `progress-indeterminate-slide` keyframe + reduced-motion fallback.

## Validation evidence

- `npx vitest run src/components/warmupSection/helpers.test.ts` — 10 passed
  (4 new cases for `isWarmupProgressIndeterminate`).
- `npx vitest run` (full web suite) — 96 files, 863 passed.
- `npm run build` — TypeScript strict + Vite build clean (EXIT=0).
- `npx eslint` on the changed files — clean.
- Live `kubectl get jobs -l app=elb-db-warmup` on `elb-cluster-01` confirmed all
  warmup Jobs `Complete`, establishing the issue was visual, not a real hang.
