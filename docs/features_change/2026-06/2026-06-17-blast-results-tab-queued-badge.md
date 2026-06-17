# BLAST Results — tab badge reads "Queued" for queued jobs (no more mixed Running/queued)

## Motivation

A BLAST job sitting in a queued phase (`queued`, `waiting_for_submit_slot`,
`waiting_for_capacity`, `capacity_reserve_lost`) rendered an inconsistent
status on the Results page: the header phase banner and the Job Details status
dot correctly read a calm grey **"queued / Waiting in queue"**, but the result
tabs (Descriptions / Graphic Summary / Alignments / Taxonomy / Files) showed a
hardcoded accent-blue **"Running"** badge. The two indicators contradicted each
other on the same screen.

### Root cause

The tab badge was gated on `resultsPending`, which is the page's `isRunning`
flag. `isRunning` (in `web/src/pages/blastResultsModel.ts`) is `true` for any
non-terminal, non-failed phase — including the queued-family phases that the
reconciler intentionally keeps with the `status="running"` sentinel. The badge
then printed a literal `"Running"` string with the accent colour regardless of
the actual phase, while every other surface used `QUEUED_PHASES` / `phaseLabel`
to collapse those phases to "queued".

## User-facing change

- The in-progress badge on the BLAST result tabs now reads **"Queued"** in the
  calm grey `--text-muted` tone while the job is in a queued-family phase, and
  keeps the accent **"Running"** only when the job is genuinely running. This
  matches the header phase banner and the Job Details status dot, so the page
  tells a single consistent story.

## API / IaC diff summary

Frontend-only, no API/IaC change. Reuses the existing `QUEUED_PHASES` single
source of truth from `web/src/constants.ts`.

- `web/src/pages/blastResults/BlastResultsTabs.tsx` — new optional
  `effectivePhase` prop; new pure `resultTabBadge(effectivePhase)` helper
  returning `{ label, color }`; badge label/colour now derive from it.
- `web/src/pages/BlastResults.tsx` — passes `effectivePhase={effectivePhase}`
  into `<BlastResultsTabs>`.
- `web/src/pages/blastResults/BlastResultsTabs.test.ts` — added unit tests for
  `resultTabBadge` (Queued grey for all queued-family phases; Running accent
  for running/empty phases).

## Validation evidence

- `npx vitest run src/pages/blastResults/BlastResultsTabs.test.ts` → 4 passed.
- `npx eslint` on the three touched files → exit 0.
- `npm run build` → built clean (only the pre-existing chunk-size warning).
