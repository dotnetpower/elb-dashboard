---
title: External-API BLAST jobs show the full database metadata block
description: Recover the workload Storage account from the BLAST db blob URL so jobs submitted via the sibling OpenAPI render the same DB sequence/letter counts and snapshot date as dashboard-submitted jobs.
tags:
  - blast
  - user-guide
---

# External-API BLAST jobs show the full database metadata block

## Motivation

A BLAST job submitted **from the dashboard** renders a rich NCBI-style detail
header: program, database title/description/molecule type, **DB sequences, DB
letters, DB snapshot, DB updated**. The same job submitted **directly against
the sibling OpenAPI** (`/v1/jobs`) rendered a sparse header — the database block
was missing the sequence/letter counts and snapshot date.

Root cause: external-API jobs never populate `infrastructure.storage_account`.
The result-page database metadata resolver
(`resolve_database_display_metadata`) needs a storage account to read the BLAST
`.njs` / `{db}-metadata.json` blobs that carry the counts and dates. With an
empty account it fell back to the small static `NCBI_DATABASE_CATALOG`, which
only knows the title/description/molecule type for `core_nt` — hence the partial
render. The information was available all along: external jobs carry the BLAST
database as a full blob URL (for example
`https://<account>.blob.core.windows.net/blast-db/core_nt/core_nt`), so the
account can be recovered from the URL itself.

## User-facing change

Jobs submitted via the sibling OpenAPI (`/v1/jobs`), and jobs synced from it
into the dashboard job table, now render the full database metadata block on the
result detail page — DB sequences, DB letters, DB snapshot, and DB updated —
matching dashboard-submitted jobs, whenever the workload Storage account is
reachable.

Query-side fields (QUERY ID / QUERY LENGTH / DESCRIPTION) remain unavailable for
direct `/v1/jobs` submissions: the sibling OpenAPI never returns the query FASTA
content, so those values cannot be derived dashboard-side. MOLECULE TYPE still
falls back to the program (for example `blastn` → DNA). Surfacing the query-side
fields would require exposing the query metadata in the
`dotnetpower/elastic-blast-azure` OpenAPI.

## API / IaC diff summary

- `api/services/blast/db_metadata.py`: new `extract_storage_account(database)`
  helper that recovers the Storage account name from a BLAST db blob URL and
  returns `""` for bare DB names / non-blob hosts; new
  `extract_trusted_storage_account(database)` that returns the recovered account
  **only** when it matches the deployment's configured workload Storage account
  (`AZURE_BLOB_ENDPOINT` host / `AZURE_STORAGE_ACCOUNT` / `STORAGE_ACCOUNT_NAME`),
  else `""`.
- `api/services/blast/external_jobs.py` (`_external_to_blast_job`): when
  `infrastructure.storage_account` is empty, derive it from the external job's
  `db` blob URL **through the trust gate** before resolving database metadata.
- `api/services/blast/job_state.py` (`_local_to_blast_job`): when
  `infrastructure.storage_account` is empty, derive it from the row's `db` or
  the synced `payload.external.db` blob URL, again **through the trust gate**.
- No response-shape change: `database_metadata` keeps its existing optional
  fields; this change only populates more of them. The frontend
  (`BlastJobHeader.tsx`) already reads `number_of_sequences`,
  `number_of_letters`, `source_version`, and `update_date`.
- No IaC change. No new RBAC: the api sidecar already holds Storage Blob Data
  Reader on the workload account; a foreign account simply fails closed
  (resolver returns `None`).

## Security: trust gate on the URL-derived Storage account

The `db` field of an external `/v1/jobs` job is influenced by whoever called
the sibling OpenAPI. Turning a raw `db` URL into an authenticated Storage call
would send the api sidecar's managed-identity token (Azure AD scope
`https://storage.azure.com/.default`, which is account-agnostic) to whatever
`<account>.blob.core.windows.net` host the URL names — an SSRF / token-exfil
vector even though the host is constrained to `*.blob.core.windows.net`.

The fix gates every URL-derived account through `extract_trusted_storage_account`,
which returns the account only if it equals the deployment's single configured
workload account; any foreign account (or an unconfigured environment) falls
back to `""`, i.e. the pre-enrichment static-catalogue behaviour. The trusted
`infrastructure.storage_account` path is unaffected — it was always empty for
external jobs, so the gate closes the new hole without regressing dashboard
jobs.

## Logs limitation for direct `/v1/jobs` jobs

Per-step timing **and** logs for dashboard-submitted jobs are produced only by
the dashboard's own submit task, which captures the live `elastic-blast` CLI
stdout/stderr into JobState history events. Direct `/v1/jobs` jobs bypass that
task entirely, and the sibling OpenAPI exposes **no log endpoint** (its status
payload carries only `kubernetes.summary`, no log content). Live AKS pod logs
(`api/services/job_logs/k8s.py`, already wired via `payload.external.k8s.job_id`)
can be followed only while the job's pods are alive; pods are deleted after the
job completes. So a completed API-submitted job has no fetchable logs by design.
Submitting through the dashboard facade remains the way to get the rich
per-step logs; adding a log endpoint to `dotnetpower/elastic-blast-azure` would
be a separate cross-repo change.

## Validation evidence

- `uv run pytest -q api/tests/test_blast_db_metadata.py api/tests/test_local_to_blast_job.py`
  → 43 passed (includes
  `test_extract_storage_account_handles_every_input_shape`,
  `test_extract_trusted_storage_account_gates_on_workload_account`,
  `test_extract_trusted_storage_account_refuses_when_unconfigured`,
  `test_local_to_blast_job_derives_storage_account_from_external_db_url`, and the
  negative `test_local_to_blast_job_refuses_foreign_external_db_storage_account`).
- `uv run pytest -q api/tests/test_external_blast_api.py api/tests/test_blast_jobs_routes.py api/tests/test_blast_db_metadata.py api/tests/test_local_to_blast_job.py`
  → 118 passed.
- `uv run ruff check` on all touched files → clean.
- Live ground truth: production job `ee0142c012c7` detail JSON carries
  `payload.external.db_version_detail.detail` with the counts
  (`number_of_sequences=125940211`, `number_of_letters=1058342797689`,
  `source_version=2026-05-26-01-05-01`) and a `db` blob URL on storage account
  `stelbdashboard3abp67bppe`, confirming the account is recoverable from the URL
  the resolver now uses.
