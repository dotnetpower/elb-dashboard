---
title: BLAST Submit wizard UX batch (shortcut hint, error-scroll, skeleton, example search, unload guard)
description: Five targeted UX improvements to the BLAST Submit wizard — submit shortcut hint, scroll-to-first-error, DB recommendation skeleton, example-picker search, and an in-flight unload guard.
tags:
  - blast
  - ui
---

# BLAST Submit wizard — UX batch

## Motivation

Continuation of the UI/UX improvement pass, focused on the BLAST Submit wizard.
Several frictions remained: the Ctrl/⌘+Enter submit shortcut was undiscoverable,
a failed submit only toasted the missing field without moving the user to it,
the "Help me choose a database" panel showed a bare spinner, long example lists
had no filter, and a tab refresh mid-submit could silently abandon the job.

> Note: three candidates from the original list were already implemented and
> left untouched — the stepper sidebar is already `position: sticky`
> ([blast-submit-layout.css](../../../web/src/theme/blast-submit-layout.css)),
> FASTA validation already updates reactively in `QuerySection`, drag-and-drop
> upload already exists, and the autosave "Saved Ns ago" indicator already
> lives in the footer. A cost/time estimate preview needs a backend estimator
> and is deferred.

## User-facing change

- **#15 Shortcut hint** — the Run BLAST button now shows a `Ctrl+↵` kbd badge so
  the existing keyboard submit is discoverable.
- **#16 Scroll to first error** — submitting with missing fields now scrolls the
  wizard to the first incomplete step (Database → Sequence → Taxonomy → Cluster
  in visual order) before showing the toast.
- **#14 DB recommendation skeleton** — the "Help me choose a database" panel
  renders an animated shimmer skeleton while the recommendation loads, instead
  of only a spinner on the button.
- **#17 Example search** — `ExamplePicker` gains a filter box (label +
  description) that appears only when a tool exposes more than six presets, with
  a "no match" hint.
- **#12 Unsaved-submit guard** — a `beforeunload` prompt now fires only while a
  submit is in flight, so a refresh/close won't abandon an in-progress
  submission. Scoped to the pending window so it never nags during editing.

## Code change summary

- [web/src/pages/BlastSubmit.tsx](../../../web/src/pages/BlastSubmit.tsx):
  scroll-to-first-incomplete-step in `handleSubmit`; `beforeunload` effect gated
  on `submitMutation.isPending`.
- [web/src/pages/blastSubmit/BlastSubmitFooter.tsx](../../../web/src/pages/blastSubmit/BlastSubmitFooter.tsx):
  `Ctrl+↵` kbd badge on the Run button (normal state only).
- [web/src/pages/blastSubmit/DatabaseRecommendPanel.tsx](../../../web/src/pages/blastSubmit/DatabaseRecommendPanel.tsx):
  shimmer skeleton while `loading`.
- [web/src/components/ExamplePicker.tsx](../../../web/src/components/ExamplePicker.tsx):
  search/filter box for long preset lists.
- [web/src/theme/glass.css](../../../web/src/theme/glass.css): `.blast-submit-kbd`,
  `.blast-db-reco__skeleton`, `.example-picker__search` styles.

## Validation evidence

- `cd web && npm run build` → clean.
- `cd web && npm test -- --run submitValidation ExamplePicker blastSubmit` →
  217 passed.
- `npx eslint` on the four changed files → clean.

No backend / API / IaC changes.
