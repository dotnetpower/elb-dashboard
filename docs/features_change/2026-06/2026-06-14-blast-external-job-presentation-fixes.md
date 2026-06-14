---
title: BLAST external-origin job presentation fixes
description: "Address the cluster of regressions surfaced by job
e1f0d24fdc74 in the moonchoi deployment: false-positive worker_lost flips,
stuck stale error codes after the sibling reports completion, citation
duplication, /file responses degrading to 503 for missing blobs, missing
storage_account column on external rows, INFO log spam, and the
job_title collapsing to the literal string \"blast\"."
tags:
  - blast
  - user-guide
---

## Motivation

A live OpenAPI-sourced BLAST job (`e1f0d24fdc74`, sibling-submitted via
`elastic-blast-azure`) on the deployed `ca-elb-dashboard` Container App
exposed a cluster of presentation defects whose common root cause was
that the **external-origin row contract** (no local Celery task, payload
under `payload["external"]`) was not consistently honoured by:

* the time-based stale-job reconciler,
* the local-row → dashboard projection,
* the sibling-sync writer,
* the citation/provenance builders,
* the storage failure classifier.

The symptoms surfaced as a `worker_lost` badge that survived sibling
completion, a citation reading "queried the the selected database
database", `/file` returning 503 for plain missing blobs, and a `blast`
literal in place of a meaningful job title.

## User-facing change

* **External-origin rows no longer flip to `worker_lost`** when the
  sibling sync window has a transient miss. The reconciler now skips
  rows whose payload carries an `external` dict and no local task id —
  the sibling is the source of truth for them.
* **Stuck `worker_lost` clears on terminal flip.** When the sibling
  reports `completed`/`succeeded` and the Table row still carries a
  stale `error_code`, the sync writes an explicit empty string so the
  row drops the badge durably.
* **Step timeline trusts the external projection.** For external rows
  the `_progress.steps` map written by a previous false-positive
  reconcile pass is ignored; the external step projection is the only
  step source.
* **`source: external_api`** appears on projected external rows (was
  hardcoded `"dashboard"`).
* **Job title falls back to the openapi job id** instead of collapsing
  to the literal string `"blast"` when the sibling lacks
  program/db/query/title metadata. Explicit titles and the normal
  program-plus-db heuristic are preserved.
* **Citation reads "queried the selected database"** (single clause)
  when no database name is known, and surfaces the external
  `db_name`/`db` when present.
* **Provenance bundle** now falls back to `payload["external"]["db_name"]`
  then `payload["external"]["db"]` for the database name, so the
  Methods paragraph and the BibTeX entry name the real database for
  external-origin runs.
* **`/api/blast/jobs/{id}/file` returns 404** (not 503) when the
  underlying blob does not exist. The classifier now matches the
  Azure SDK's `ResourceNotFoundError` by type name and the
  `ErrorCode:BlobNotFound` substring that the SDK's `str(exc)` actually
  renders.
* **External rows carry `storage_account`** on the projected payload
  (derived from the trusted-workload-account check on the `db` URL),
  so the sibling-sync writer fills the Table column and the dashboard
  no longer flows through the "JobState has no recorded account"
  fallback on every result-route call.
* **Two cross-check log lines demoted** from INFO to DEBUG (issue
  #19): "no JobState row" and "no recorded account" — both fire on
  legitimate steady-state paths (recently-submitted rows; external
  sync rows) and flooded App Insights without operator value.

## API/IaC diff summary

Backend only. No infrastructure or Bicep changes; no sidecar or
container image changes.

* api/services/storage/failure_classifier.py — detect
  `ResourceNotFoundError` / `BlobNotFoundError` / `ContainerNotFoundError`
  by class name plus the `BlobNotFound` substring fallback.
* api/services/blast/citation.py — dedupe the "queried the …
  database" clause when the database name is unknown.
* api/services/blast/provenance.py — fall back to
  `payload["external"]["db_name"]` / `["db"]` for the database name.
* api/tasks/blast/reconcile_task.py — skip time-based `worker_lost`
  on rows that have no local task id and carry a payload `external`
  dict.
* api/services/blast/job_state.py — for external-origin rows: drop
  the stale `_progress` steps, fill blank `db` from
  `external.db_name`/`external.db`, clear `worker_lost`-style
  `response_error_code` on terminal-success status, and emit
  `source: external_api`. Log demotions for the two cross-check
  lines.
* api/services/blast/external_jobs.py — on terminal-success flip,
  write `error_code=""` so the next read sees a clean row.
* api/services/blast/external_job_projection.py — derive
  `infrastructure.storage_account` from the trusted-db URL even on
  the list view; fall back to an openapi-id-based `job_title`
  instead of the literal `"blast"` when no sibling metadata exists.

## Validation evidence

* `uv run pytest -q api/tests` — 3522 passed, 3 skipped (was 3510;
  +12 new tests cover the BlobNotFound classifier path, the citation
  dedupe, the provenance external fallback, the reconcile external
  skip, the sibling-sync stale-error-code clear, the storage-account
  derivation with trust gate, and the title fallback paths).
* `uv run ruff check api` — all checks passed.
* Diff audit: `git status --short` confirms only the seven backend
  modules and five test modules above are dirty (alongside the
  pre-existing message-flow work in the working tree which is not
  part of this change).

## Out of scope

Issues #9 (sibling DELETE 200 vs body), #10 (sibling-side error
echo), #14 (`resource_group: rg-elb-cluster` literal) and #18
(sibling status sync timing) live in the
`dotnetpower/elastic-blast-azure` sibling and are filed for that
repo. Issue #20 (frontend retry backoff on transient errors) is
deferred — Fix #1 already turns the dominant "missing blob" 503
into a 404 the SPA does not retry, so the backoff burst should
materially shrink without a frontend change.
