---
title: Fix "Could not load input.fa" query preview for external BLAST jobs
description: The Run details prepare-step query preview now resolves the query blob for OpenAPI / Service Bus jobs (stored at queries/<openapi_id>.fa) instead of guessing queries/<job_id>/input.fa, so the original query is shown instead of "Could not load input.fa".
tags:
  - blast
  - ui
---

# Fix "Could not load input.fa" query preview for external BLAST jobs

## Motivation

On the Run details page of an OpenAPI / Service-Bus-submitted BLAST job, the
**Prepare Run** step showed "✓ Run prepared." followed by **"Could not load
input.fa"**, and the query was not viewable — making a perfectly normal run look
broken. Investigation of a live job (`4eb504769791`, blastn / core_nt) confirmed:

- The query **was** uploaded and split correctly (`query_batches/batch_000.fa`
  = 76 B in Storage), the search ran on all 5 DB shards, and the result
  ("No significant similarity found", empty 58 B shard outputs) is a
  **legitimate BLAST result for that query — not a bug**.
- The only real defect was the **query preview failing to load**.

Root cause: the prepare-step preview (`FilePreview`) resolved the query blob
with `resolveUploadQueryBlobName`, which for an external job (no top-level
`query_file` on the row — those live under `payload.external`) fell back to
guessing `queries/<job_id>/input.fa`. External jobs store the inline FASTA at
`queries/<openapi_id>.fa` (the sibling elastic-blast-azure plane uploads it
there), so the guessed path 404'd. The frontend sent that guessed path as the
explicit `name=...`, and the backend `blast_job_file` route then tried only that
one path. The authoritative `blast_job_query` (Edit search) route already
reconstructs `<openapi_id>.fa` for external jobs — the file/preview route did
not, so the two surfaces disagreed.

## User-facing change

The Run details prepare-step now shows the original query FASTA for external
(OpenAPI / Service Bus) jobs instead of "Could not load input.fa". Dashboard-
submitted jobs are unchanged.

## API / IaC diff summary

No HTTP contract change. Two complementary fixes:

- **Backend** — `api/services/blast/job_state.py` `_job_query_blob_path`: when
  the job row has no top-level `query_file` / `query_blob_url`, reconstruct the
  external convention `<openapi_id>.fa` from `payload.external.job_id` (guarded
  against `/` and `..`), mirroring `blast_job_query`. `blast_job_file` then
  tries `queries/<openapi_id>.fa` first for `name=input.fa`.
- **Frontend** — `web/src/components/BlastStepTimeline/StepLogSection.tsx`:
  `resolveUploadQueryBlobName` no longer guesses `queries/<job_id>/input.fa`; it
  returns an authoritative path or `undefined`. The prepare preview renders even
  without a resolved blob name, so `readJobFile` sends `name=input.fa` and the
  backend resolves the real blob (its candidate list already covers
  `<job_id>/input.fa` for dashboard jobs and now `<openapi_id>.fa` for external
  jobs).

## Validation evidence

- `uv run ruff check api` — clean.
- `uv run pytest -q api/tests/test_smoke.py -k "job_file or job_query"` — 7
  passed, including the new `test_blast_job_file_reads_external_openapi_query`.
- Wider sweep `test_smoke.py test_blast_jobs_routes.py test_blast_results_parser.py test_external_job_projection.py test_blast_tasks.py`
  — 287 passed.
- `cd web && npm run build` — succeeds; `BlastStepTimeline` 38 tests pass.

## Follow-up (not in this change)

- The Run details header still shows **Query ID: —** for external jobs (the
  defline-derived label is not projected onto the external job header). That is
  a separate, cosmetic gap tracked apart from the preview fix; the query content
  is now viewable in the prepare step regardless.
