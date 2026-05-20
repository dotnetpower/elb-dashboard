# BLAST job header — "Download all" gated on completed results

## Motivation

When a user submitted a BLAST job and landed on `/blast/jobs/{id}?submitted=1&...`,
the NCBI-style header rendered the **Download all ▾** combo button even while
"Loading job details..." was still visible (job not yet fetched) and during the
warming_up / configuring / staging / submitting / running phases. The button
implied results were ready, which is misleading. The download endpoints would
also return no aggregated rows because the result manifest does not exist until
the export phase finishes.

The download UI inside `JobDetailsCard` already had the right gate — it is
mounted only inside `showCompletedMetrics`, which requires the job to be loaded,
phase to be `completed`, the job not failed, and at least one result file
present. The header was the only place still using the loose
`Boolean(subscriptionId && storageAccount)` gate.

## User-facing change

- The header **Download all ▾** combo no longer appears while the job is
  loading, queued, warming up, configuring, staging, submitting, running,
  cancelled, deleted, or failed without output files. It appears only once the
  job has reached `completed` with at least one downloadable result file —
  matching the behaviour of the existing download buttons inside the job
  details metrics card.
- No change to the existing in-card download buttons; both surfaces now use
  the same gate.

## API / IaC diff summary

- [web/src/pages/BlastResults.tsx](../../../web/src/pages/BlastResults.tsx):
  changed `hasExportTargets={Boolean(subscriptionId && storageAccount)}` to
  `hasExportTargets={state.showCompletedMetrics}` when rendering
  `<BlastJobHeader />`. No props, types, or other components touched.

No backend, IaC, or API contract changes.

## Validation evidence

- `cd web && npx tsc --noEmit` → exit 0.
- Manual: opening `/blast/jobs/{id}?submitted=1&...` for a job in the
  `warming_up` / `running` / `loading` state no longer shows **Download all ▾**
  in the header; the same job after `phase === "completed"` with result files
  shows the combo (and the in-card buttons), matching the existing
  `JobDetailsCard` behaviour.
