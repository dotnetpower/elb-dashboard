# BLAST results page — restore DB metadata rows

## Motivation

User report: "DB sequences, DB letters, DB snapshot 정보가 blast 결과에
누락되었어. 플래그 설정이 false 로 된것 같은데". The BLAST results page
(`/blast/jobs/:jobId`) header had stopped rendering the database metadata
rows (`DB title`, `DB description`, `DB molecule / DB updated`,
`DB sequences / DB letters`, `DB snapshot`) — they were silently empty for
every job, regardless of whether the underlying `.njs` metadata existed in
the workload Storage account.

## Root cause

The polling/detail-latency hardening done on 2026-05-21
([2026-05-21-blast-status-latency.md](./2026-05-21-blast-status-latency.md))
flipped `useBlastResultsState.jobQuery` to call
`blastApi.getJob(jobId, { includeDatabaseMetadata: false })` so the 3–5 s
poll stays cheap. Resolving database metadata on every poll round-trips to
Azure Storage (`{db}/{db}.njs` + `{db}-metadata.json` reads via
`api.services.blast_db_metadata.resolve_database_display_metadata`) and was
the biggest single cost in that path.

That change correctly cut poll latency, but `BlastResults.tsx` was still
reading the metadata exclusively from the polling response
(`databaseMetadata={job?.database_metadata ?? null}`). Because the polling
query now intentionally omits the field, `BlastJobHeader` permanently sees
`databaseMetadata == null` and skips the gated rows:

- `databaseMetadata?.title` → "DB title"
- `databaseMetadata?.description` → "DB description"
- `databaseMetadata?.molecule_type || update_date` → "DB molecule" / "DB
  updated"
- `dbSequenceCount || dbLetterCount`
  (`databaseMetadata.number_of_sequences/letters`) → "DB sequences" / "DB
  letters"
- `databaseMetadata?.source_version` → "DB snapshot"

So the "flag" that was effectively `false` was
`include_database_metadata=false` on the polling endpoint — not a Vite
feature flag.

## Change

Frontend-only. The polling query stays light; metadata is fetched once via
a sibling React Query with `staleTime: Infinity` and no refetch triggers.

- `web/src/pages/blastResults/useBlastResultsState.ts`
  - Added `databaseMetadataQuery` that calls
    `blastApi.getJob(jobId, { includeDatabaseMetadata: true })` exactly
    once per page load, with `staleTime: Number.POSITIVE_INFINITY`,
    `gcTime: 1h`, `refetchOnWindowFocus/Reconnect: false`,
    `refetchInterval: false`.
  - Exposed `databaseMetadata =
    databaseMetadataQuery.data?.database_metadata
    ?? job?.database_metadata ?? null` on the hook return.
- `web/src/pages/BlastResults.tsx`
  - `<BlastJobHeader databaseMetadata={state.databaseMetadata} />` instead
    of reading off the polling `job` directly.

No backend changes. The
`include_database_metadata=true` code path on `/api/blast/jobs/{job_id}`
already exists and is the default; it was just no longer being requested
by the SPA.

## Validation

- `cd web && npm run build` → `✓ built in 10.37s`, no TypeScript errors.
- `get_errors` clean on both touched files.
- Behaviour is reproducible in the browser by opening
  `http://127.0.0.1:8090/blast/jobs/<id>` for any job whose database has
  `.njs` / `-metadata.json` blobs in `blast-db/` (e.g. the
  `core_nt`/`core_nt_shard_*` jobs the repo memory references): the
  header now shows the full "DB title / DB description / DB molecule /
  DB updated / DB sequences / DB letters / DB snapshot" block again
  while the 5 s poll keeps returning the lightweight payload.

## Rollback

`git revert` the single commit that ships these two file edits — there
is no schema or backend change to undo.

## Out of scope

- The `getJob` endpoint itself, the `include_database_metadata` query
  param, and the polling cadence — unchanged.
- Other consumers of `BlastJobSummary.database_metadata` (e.g.
  external/legacy jobs path, `_external_to_blast_job`) — unchanged; they
  already pass `include_database_metadata=True` by default.
- No Azure redeploy required.
