# AKS card — Jobs roster collapses to a single-line row

## Motivation

The AKS card's Jobs roster rendered each job over **two visual lines**: a
title row on top and a chip row (program · db · query) below, with a
stacked time block (age over duration) on the right. The density was
hard to scan at a glance — every entry felt like a card instead of a
row.

User asked for a single-line format that reads left-to-right like a
log line:

```
blastn | 16S_ribosomal_RNA | 20260527-0839 E. coli 16S rRNA - NR_024570.1 | FAILED | 1h 1m(age) | 1h 0m(duration)
```

## User-facing change

* `web/src/components/cards/ClusterPulse/JobLine.tsx` row geometry is
  now a single horizontal line:
  - **Identity cell**: `{program} | {db} | {title}` inline (mono font,
    accent-coloured program, primary-coloured db, bold title). Only
    `title` ellipses; program and db keep their width.
  - **Time cell**: horizontal `{age}(age) | {duration}(duration)` with
    the `ago` suffix stripped from the age label so it reads as a span,
    not a tense.
* The standalone query chip is dropped — the title already encodes the
  query in every observed payload (`fallbackJobTitle` always glues
  query into the title when `job_title` is absent), so the chip was
  duplicative.
* Grid template `time` column widened from `104px` → `160px` (with /
  without user variant), with no other layout shifts. Identity cell
  height reduced from ~32 px (two stacked lines) to ~22 px (one line),
  letting more jobs fit into the roster's max-height before the "More
  jobs +N" affordance appears.
* Mobile layout (≤ 480 px breakpoint in `dashboard-layout.css`) is
  unaffected — the existing `.pulse-job-row` mobile override still
  stacks identity / status / time onto separate lines.

## API / IaC diff summary

None. Pure presentation change inside one component file.

## Validation evidence

* `cd web && npx tsc --noEmit` — clean.
* `cd web && npx eslint src/components/cards/ClusterPulse/JobLine.tsx`
  — clean.
* `cd web && npm test -- --run` — **363 tests pass, 50 files**. No
  test mutations needed; no test asserts the JobLine row's stacked
  geometry (the only `pulse-job-*` references live in
  `theme/dashboard-layout.css` and stay intact).
* Diff scope: 1 file (`JobLine.tsx`), +81 / −78 lines. The unused
  `ChipMono` / `ChipTone` helpers + `prettifyQueryLabel` import were
  removed; `PipeSep` + `stripAgoSuffix` added. `prettifyQueryLabel`
  itself stays exported from `helpers.ts` because it has no other
  consumer to clean up safely in this change.
