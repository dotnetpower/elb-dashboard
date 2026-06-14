---
title: API-submitted BLAST jobs no longer stuck showing "blast" with no database
description: Self-heal degenerate program/db/job_title columns on external OpenAPI job rows in the jobs list / session browser.
tags:
  - blast
  - user-guide
---

# 2026-06-14 — API jobs no longer stuck as "blast" with no database

## Motivation

BLAST jobs submitted through the external OpenAPI plane (`POST /v1/jobs`) showed
up in **Recent searches / the session browser** with the job title rendered as
the literal string `blast` and **no database** — even though the same job's
detail page showed the correct program and database.

Root cause (confirmed against the live `jobstate` table via the deployed api
sidecar):

1. When an external job is first discovered, `collect_and_sync_external_jobs`
   upserts it into Azure Table Storage. If that first `/v1/jobs` row was a
   transient one that did not yet carry `program`/`db` (e.g. observed right after
   an OpenAPI pod restart), the dedicated columns were persisted with the
   canonical defaults: `program = "blast"`, `job_title = "blast"`, `db = ""`.
   (`canonical_job_metadata` reads the payload *top level*, not its nested
   `external` key, so a `{"external": …}` payload yields the defaults.)
2. The sync **update path only backfilled the four scope columns**
   (`subscription_id` / `resource_group` / `cluster_name` / `storage_account`) —
   it never re-derived `program` / `db` / `job_title` / `query_label`. So once
   the sibling list later carried the real values, the row stayed stuck.
3. With `BLAST_JOBS_SHARED_VISIBILITY` on, the stale `owner_oid=""` row is listed
   as a "local" Table row and **wins the merge**; the fresh external projection
   (which computes `blastn - core_nt`) is deduped out.
4. The list view reads the Table columns directly (`include_payload=False`), so
   `_local_to_blast_job`'s `payload.external` fallback never ran and the
   degenerate columns surfaced verbatim. The SPA's `job_title` is truthy
   (`"blast"`), so its own fallback could not kick in either.

## User-facing change

* The jobs list / session browser now self-heals these rows. Once the authoritative
  `/v1/jobs` list reports a real program/db, the next poll rewrites the degenerate
  `program` / `db` / `job_title` / `query_label` columns, so the API job shows e.g.
  `blastn - core_nt` with its database instead of `blast` / `—`.
* Rows that already carry good metadata are never overwritten — the heal only
  fills a column that is the degenerate canonical default (`program`/`job_title`
  in `{"", "blast"}`, empty `db`, empty `query_label`) and only when the upstream
  carries a real value.
* Convergence is automatic within one cache cycle (jobs-list cache fresh TTL 10 s
  / stale 70 s with background revalidation), no user action required.

## API / IaC diff summary

No API surface or IaC change. Internal only:

* `api/services/state/repository.py` — `JobStateRepository.update()` gains four
  optional, verbatim-written keyword args (`job_title`, `program`, `db`,
  `query_label`) mirroring the existing scope-backfill args (default `None` =
  unchanged).
* `api/services/blast/external_jobs.py`
  * `_sync_external_jobs_to_table` update path: heal degenerate identity columns
    from the fresh projection (fill-only-when-degenerate, never clobber good
    values).
  * `collect_and_sync_external_jobs`: rows that are already a local Table row this
    request (pre-seeded into `seen`) are still passed to the sync for a metadata
    heal pass (no duplicate display, no detail-enrichment budget spent), so
    existing stale rows converge instead of being skipped by de-duplication.

## Validation evidence

* `uv run pytest -q api/tests` → **3524 passed, 3 skipped**.
* New tests in `api/tests/test_external_blast_api.py`:
  * `test_sync_external_jobs_heals_degenerate_identity_columns` — a row stored as
    `program="blast"`, `db=""`, `job_title="blast"` is healed to `blastn` /
    `core_nt` / `blastn - core_nt`.
  * `test_sync_external_jobs_does_not_overwrite_good_identity_columns` — a row with
    real metadata triggers no write.
* `uv run ruff check api/services/blast/external_jobs.py api/services/state/repository.py` → clean.
* Live root-cause confirmation: `jobstate` rows `509a3347a4d9` / `e1f0d24fdc74`
  carried `program="blast"`, `db=""`, `job_title="blast"` while their
  `payload.external` and the live `/v1/jobs` both carried `program="blastn"` and
  the full `core_nt` / `16S_ribosomal_RNA` db URL.
