---
title: BLAST example picker refreshes the auto job title on switch
description: Switching query examples now refreshes the auto-generated job title while preserving a manually edited title.
tags:
  - blast
  - ui
---

# BLAST example picker — auto job title refreshes on example switch

## Motivation

The New Search query example picker auto-stamps a job title
(`YYYYMMDD-hhmm <example label>`) the first time an example is loaded into an
empty form. Previously the title was only set when `job_title` was empty, so a
researcher who picked example A and then switched to example B kept the
**example A** label in the job title — a stale, misleading default that had to
be cleared by hand before it would refresh.

## User-facing change

- Loading an example now refreshes the auto-generated job title when the title
  is empty **or** still equal to the title the picker generated for a
  previously chosen example.
- A title the researcher typed by hand is left untouched. Detection is done by
  remembering the last auto-generated value in a `useRef`; anything that does
  not match it is treated as a manual edit and preserved.

## Code change summary

- `web/src/pages/blastSubmit/QuerySection.tsx`
  - Added `lastAutoTitleRef` (`useRef<string | null>`) to track the last
    auto-generated job title.
  - `loadExample()` now refreshes the title via `buildGeneratedJobTitle` when
    the current title is empty or equals `lastAutoTitleRef.current`, and stores
    the new value back into the ref.

No API, IaC, or backend changes.

## Validation evidence

- `cd web && npm run build` — clean (exit 0).
- `npx vitest run src/pages/blastSubmit` — 11 files, 150 tests passed.
- `npx eslint src/pages/blastSubmit/QuerySection.tsx` — clean (exit 0).
- Consumer check: `loadExample` is only referenced by the example modal's
  `onSelect` in the same component; no external consumer depends on the old
  set-only-when-empty behaviour.
