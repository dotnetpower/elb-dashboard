# Failed Job Run Details Tab

## Motivation

Failed BLAST job links could open with `?tab=descriptions`, which put the user on an empty results-oriented view even though the useful failure evidence is in the execution timeline.

## User-facing change

When a failed job is opened on a result analytics tab such as Descriptions, the page now replaces the URL tab with `run` once the failed state is known. Operator tabs such as Files and Run details are left alone, and a later manual tab click is not trapped.

## API/IaC diff summary

- Added a frontend tab-routing helper for failed BLAST jobs.
- No API or IaC changes.

## Validation evidence

- `cd web && npm run test -- src/pages/blastResults/BlastResultsTabs.test.ts src/pages/blastResultsModel.test.ts`
- `cd web && npm run build`