# Run details header shows the query for API-submitted (/v1/jobs) jobs

## Motivation

A BLAST job submitted directly through the sibling OpenAPI `/v1/jobs` endpoint
showed **Query: —** in the Run details header while running (and after). A
researcher could not tell which query the job ran.

Root cause is a frontend projection gap, not missing data:

* The backend job projection always sets a top-level `query_label`
  (`api/services/blast/external_job_projection.py` → `metadata["query_label"]
  or "query.fa"`), and nests the upstream job under `payload.external`.
* `BlastJobHeader`'s query resolution only read from `jobPayload`
  (`= job.payload`). For an external job that payload is `{ external: {...} }`,
  so `query_id` / `query_name` / `query_label` / `query_metadata` /
  `query_file` are all absent at the payload top level → the header fell back
  to `—`.
* Every other surface (e.g. the ClusterBento job cards) already resolved
  `j.query_label ?? externalQueryLabel(j)`; only the Run details header
  ignored both the top-level `query_label` and the nested external payload.

Note: a direct `/v1/jobs` submit stores no query identity upstream (the running
status response carries only `job_id` / `status` / `progress_pct` /
`created_at`), so the honest best value is the backend-resolved `query_label`
(the remembered defline for dashboard-bridge submits, or the generic `query.fa`
placeholder). Showing it is still strictly better than `—`.

## User-facing change

The Run details header now shows the query label for `/v1/jobs` API-submitted
jobs (while running and after), instead of `—`. Dashboard-submitted jobs are
unchanged (their payload query fields still win).

## API / IaC diff summary

- `web/src/pages/blastResults/BlastJobHeader.tsx` — new exported pure helper
  `resolveQueryHeaderId(jobPayload, queryLabel)`: dashboard payload identity →
  external `payload.external.query_file`/`query`/`query_blob_url` (basenamed) →
  top-level `query_label` fallback. New optional `queryLabel` prop.
- `web/src/pages/BlastResults.tsx` — pass `queryLabel={job?.query_label ??
  null}` to the header.
- No backend / IaC change (the data was already present; only the SPA
  projection changed).

## Validation evidence

- `web/src/pages/blastResults/BlastJobHeader.test.ts` — 6 new
  `resolveQueryHeaderId` cases (dashboard identity, query_metadata record,
  query_file basename, external dig, external `query` fallback, query_label
  fallback, null when absent). 10 passed.
- `npx tsc --noEmit` clean; `npx eslint` clean; `npm run build` passes.

## Note

Frontend-only change validated by vitest + build — no redeploy needed to verify
correctness; it ships when the frontend image carrying it is next deployed.
