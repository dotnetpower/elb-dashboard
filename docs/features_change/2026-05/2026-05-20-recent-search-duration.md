# Recent Search Duration

## Motivation

Recent BLAST searches already show each run result, but the list did not expose how long the search took.

## User-facing change

The Recent searches table now shows a secondary runtime line in the Time column. Completed and failed searches show `Duration`, while active searches show live `Elapsed` time.

## API and IaC diff summary

No API or IaC changes. The frontend derives the value from the existing `created_at` and `updated_at` job timestamps.

## Validation evidence

- `cd web && npm run build`
- Browser smoke on `http://localhost:8090/blast/jobs` confirmed rows render `Duration` labels without layout overlap.