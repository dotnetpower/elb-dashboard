---
title: Edit search loads the original query for external (OpenAPI) jobs
description: Rehydrate the query sequence into the New BLAST search form when editing a succeeded or failed job that was submitted through the OpenAPI plane.
tags:
  - blast
  - user-guide
---

# Edit search loads the original query for external jobs

## Motivation

Clicking **Edit search** on a succeeded or failed BLAST job opens the New BLAST
search form pre-filled with the original configuration, including the query
FASTA. This worked for dashboard-submitted jobs (their persisted payload carries
a `query_file` pointer the api sidecar streams back), but the query textarea was
left **empty** for jobs that originated from the sibling OpenAPI execution plane.

External jobs project their record under `payload.external` and carry no
top-level `query_file`, and the sibling plane stores no query field on the job
row at all — it only uploads the inline FASTA to `queries/<job_id>.fa`. As a
result the Edit search rehydration both skipped the fetch (frontend gate) and
could not locate the blob (backend), so the researcher had to re-paste the
sequence by hand.

## User-facing change

Editing a succeeded or failed external BLAST job now loads the original query
sequence into the form, matching the behaviour for dashboard-submitted jobs. If
the original FASTA is genuinely unavailable (blob removed, over the 5 MiB cap,
or the db points at an untrusted Storage account), the form still opens and a
warning toast explains why the query was not loaded.

## API / IaC diff summary

* `GET /api/blast/jobs/{job_id}/query` ([api/routes/blast/jobs.py](../../../api/routes/blast/jobs.py)):
  when the job row has no `query_file`/`query_blob_url` but is an external job
  (`payload.external` present), reconstruct the sibling's `queries/<job_id>.fa`
  upload convention and recover the Storage account from the external db URL
  behind the existing trusted-account gate (`extract_trusted_storage_account`).
  No response shape change; no SAS token is issued to the browser.
* `BlastJobHeader` Edit search gate
  ([web/src/pages/blastResults/BlastJobHeader.tsx](../../../web/src/pages/blastResults/BlastJobHeader.tsx)):
  also attempts the query fetch when the payload is an external projection
  (`payload.external`).

## Validation evidence

* `uv run pytest -q api/tests/test_blast_jobs_routes.py` — 14 passed, including
  the new `test_blast_job_query_reconstructs_external_blob` (asserts the
  reconstructed `queries/job-q.fa` path) and
  `test_blast_job_query_external_404_when_storage_account_untrusted` (asserts the
  trusted-account gate prevents reaching the Storage SDK for a foreign account).
* `uv run pytest -q api/tests/test_external_query_labels.py api/tests/test_blast_submit_accession.py` — green.
* `uv run ruff check api/routes/blast/jobs.py api/tests/test_blast_jobs_routes.py` — clean.
* `cd web && npm run build` — succeeds.
</content>
</invoke>
