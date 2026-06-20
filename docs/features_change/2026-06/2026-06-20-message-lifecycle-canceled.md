---
title: Message lifecycle shows canceled (not pending/delivered) for failed jobs
description: On a terminally-failed BLAST job the Message lifecycle card now renders the success-path stages (Running, Succeeded, Result delivered) as grey "canceled" instead of pending clocks or a green "Result delivered" check.
tags:
  - ui
  - blast
---

# Message lifecycle shows canceled for failed jobs

## Motivation

On the Recent searches job-detail **Run details → Message lifecycle** card, a
job that terminally **Failed** still rendered:

* **Result delivered** as a green success check with a timestamp — implying a
  result was delivered, when the job actually failed; and
* **Running** / **Succeeded** as grey **pending** clocks — implying those
  stages were still upcoming, when the success branch was never going to run.

The renderer keyed purely on "was this stage reached?": `completion_published`
is published even for a failure (so it showed as done ✓), and the unreached
success stages defaulted to `pending`.

## User-facing change

* When a job is terminally failed (`failed` / `dead_letter` reached), the
  success-branch stages render as grey **canceled** (a `Ban` icon + the text
  "canceled"):
  * **Result delivered** (`completion_published`) — no result was delivered, so
    it is canceled rather than a green success check.
  * **Running** / **Succeeded** — skipped by the failure, so canceled rather
    than pending.
* The **Failed** row itself is unchanged (red dot + timestamp).
* Succeeded jobs and in-flight jobs are unchanged (reached → done, unreached →
  pending).

## API/IaC diff summary

* [web/src/pages/blastResults/messageTraceModel.ts](../../../web/src/pages/blastResults/messageTraceModel.ts)
  — new pure helpers `traceTerminallyFailed()` and `stageDisplayState()`
  (returns `done | failed | canceled | pending`).
* [web/src/pages/blastResults/MessageTraceCard.tsx](../../../web/src/pages/blastResults/MessageTraceCard.tsx)
  — render the four states (canceled → grey `Ban` + "canceled").
* No backend / API change (the trace contract is unchanged; this is a
  presentation-state mapping).

## Validation evidence

* `npx vitest run src/pages/blastResults/messageTraceModel.test.ts` → 15 passed
  (new cases: failed job → result-delivered canceled, running/succeeded
  canceled; succeeded job → done; in-flight → pending).
* `cd web && npm run build` → built clean; `npx eslint` on the three files →
  clean.
* Live simulation on the deployed failed-job page confirmed the Running /
  Succeeded / Result delivered rows read "canceled" — screenshot captured in
  the session.
