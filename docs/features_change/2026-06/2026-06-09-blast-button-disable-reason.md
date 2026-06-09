---
title: BLAST Run button always shows a reason when disabled
description: Surface permission denial and a defensive catch-all in the submit checklist so the Run BLAST button is never silently greyed out.
tags:
  - blast
  - ui
---

# BLAST Run button — no more silent disable

## Motivation

The "Run BLAST" button could become disabled with **no visible validation
warning**. The most common cause was a resolved RBAC probe returning
`can_submit_blast = false`: `effectiveCanSubmit` flipped to `false`, the button
greyed out, but the only explanation lived in the button's hover `title`. A
researcher who never hovered just saw a dead button. A second (rarer) path —
any `canSubmit` condition without a matching checklist entry, e.g. an empty
`form.program` — produced the same silent state.

## User-facing change

- When the caller lacks `can_submit_blast` at the selected cluster scope, the
  reason now appears as a first-class entry in the "Required before submitting"
  checklist (footer + summary rail), not only on hover.
- A defensive catch-all renders a "Submission blocked" line whenever the Run
  button is disabled but no checklist entry and no pre-flight panel explain why.
  The Run button can no longer be disabled without an on-screen reason.

## Code summary

- [web/src/pages/BlastSubmit.tsx](../../../web/src/pages/BlastSubmit.tsx) —
  build `submitMissing` (validation checklist + permission entry) and pass it to
  the footer/rail.
- [web/src/pages/blastSubmit/BlastSubmitFooter.tsx](../../../web/src/pages/blastSubmit/BlastSubmitFooter.tsx),
  [web/src/pages/blastSubmit/SubmitSummaryRail.tsx](../../../web/src/pages/blastSubmit/SubmitSummaryRail.tsx) —
  add the "Submission blocked" catch-all using the existing `runTitle`.

## Validation

- `cd web && npm run build` (type-check clean).
- `npx vitest run src/pages/blastSubmit` — 207 tests pass, including
  `submitValidation.test.ts`.
