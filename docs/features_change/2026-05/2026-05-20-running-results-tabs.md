# Running Result Tabs

## Motivation

Running or queued BLAST jobs could open on result-oriented tabs before final output files were available. Those tabs showed empty or degraded result states, which looked like a failure even when the job was still executing normally.

## User-facing Change

- Running jobs opened without an explicit `tab` query now move to the Run details tab.
- Result-oriented tabs display a `Running` badge while the job is still active.
- Descriptions, Graphic Summary, Alignments, and Taxonomy show a neutral running/preparing state instead of degraded-result messaging while output is not ready.
- Result analytics queries wait until the job status is known, avoiding a misleading empty-state flash during initial load.

## API/IaC Diff Summary

- Frontend-only change under `web/src/pages/BlastResults.tsx` and BLAST result tab components.
- No API or infrastructure change.

## Validation Evidence

- `npm run build` in `web/`: passed.
- Browser check on live job `6ee67a1b-efe7-4c7f-a613-de6714e4b5fb`: opening without `?tab` redirected to `?tab=run`; result tabs showed running/loading states without `Results are degraded` while the job was active.
- Live job completed successfully after the check: API status `completed`, Kubernetes summary `pods=10`, `succeeded=10`, `failed=0`, terminal exec `exit_code=0`.