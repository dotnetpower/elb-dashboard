# 2026-06-10 — BLAST Execution Steps: follow the active step, not the page bottom

## Motivation

While a BLAST job runs (`/blast/jobs/<job_id>?tab=run` → **Execution Steps**
card), the live-log auto-scroll felt inaccurate: it would not keep the
currently-running step's latest output in view.

Root cause: the timeline always renders **all** phase rows
(`preparing → … → running → exporting_results → completed`), so the active
step sits in the *middle* of the list with the still-pending step rows
rendered below it. `useStickToBottom` scrolled to the **document bottom**,
i.e. the bottom of the empty pending `completed` row — not the active step
where the live log is actually growing. The user saw a stack of empty
pending rows pinned to the viewport bottom instead of the live log.

## User-facing change

The Execution Steps live-follow now tails the **active step row** (or the
failed step's error block), GitHub-Actions style: the latest log line of the
running step stays just above the viewport bottom, with a small margin so a
sliver of the next pending step remains visible. Manual scroll-up still
pauses auto-follow and returning to the tail re-arms it. Completed/failed
jobs with no active step keep the previous document-bottom behaviour.

## Implementation summary

- `web/src/hooks/useStickToBottom.ts`
  - New optional `anchorSelector` param. When it resolves to an element the
    hook follows that element's bottom edge instead of the document bottom;
    the last match wins so the lowest meaningful row is tailed.
  - New pure helpers `shouldFollowAnchor()` (follow/pause decision against
    the anchor bottom) and `anchorFollowTarget()` (clamped scrollTop that
    aligns the anchor bottom to 24 px above the viewport bottom).
  - The manual-scroll-away detector and the rAF scroll requester now use the
    anchor-aware decision/target so an anchor-aligned auto-scroll is not
    misread as a manual scroll-away.
- `web/src/components/BlastStepTimeline/StepRow.tsx` — the active/error step
  row is tagged `data-blast-follow-anchor="true"`.
- `web/src/pages/blastResults/ExecutionStepsCard.tsx` — passes
  `anchorSelector='[data-blast-follow-anchor="true"]'` to the hook.

## Validation

- `cd web && npm test -- --run useStickToBottom` — 13 passed (new
  `shouldFollowAnchor` / `anchorFollowTarget` geometry tests included).
- `cd web && npm test -- --run` — full suite 777 passed (84 files).
- `cd web && npm run build` — clean production build.
