---
title: prepare-db AKS progress counts only this run's blobs so updates no longer jump to 12/15 instantly
description: The AKS-fanout prepare-db live progress counted every blob under the DB prefix, so an update of a DB that already had most files on disk showed near-complete progress the moment it started. It now counts only blobs (re-)committed in the current run.
tags:
  - blast
  - ui
---

## Motivation

When updating an already-downloaded BLAST DB via the AKS-fanout path, the
progress bar jumped to a near-complete count the instant the update started —
e.g. `Copying 12 / 15 files` for `16S ribosomal RNA` at 0 s — then barely moved
while azcopy actually re-fetched everything.

### Root cause

The live progress signal (`copy_status.success` / `bytes_done`) came from
`_count_staged_blobs`, which counted **every** blob under `<db>/`. On an update,
the previous snapshot's files are still on disk, and the AKS pods re-download on
top of them (`azcopy copy … --overwrite=true`). So a DB that already had 12 of
15 files reported `12 / 15` (~80 %) immediately, even though all 15 were being
re-fetched from scratch. (The server-side copy path was unaffected — it polls
each blob's `copy.status` for the current run, which starts at 0.)

## User-facing change

The AKS-fanout progress bar now climbs from 0 honestly for both fresh downloads
and updates. The byte-based download-speed / ETA likewise reflects only bytes
landed in the current run instead of being inflated by pre-existing data.

## API / IaC diff summary

`api/tasks/storage/prepare_db_via_aks.py`:

* `_count_staged_blobs(container, db_name, *, since=None)` — new optional
  `since` filter. When set, blobs whose `last_modified` predates `since` are
  excluded (a blob with no `last_modified` is still counted; real Azure always
  sets it). `since=None` keeps the unfiltered full inventory used by the orphan
  reconciler (`api/services/storage/orphan_prepare_db.py`), which intentionally
  wants the total on-disk count.
* `_on_job_progress(..., since=None)` — threads `since` into the count.
* The task captures `progress_since = datetime.now(UTC) - 120 s` at start and
  passes it to the progress callback. The 120 s margin absorbs worker/Storage
  clock skew; pre-existing update blobs are days old, so the margin can never
  accidentally re-include them.

No frontend change: `BlastDbRow.tsx` already tracks the max `success` seen
(monotonic), so a value that now climbs from 0 renders a smoother bar.

## Validation evidence

* `uv run pytest -q api/tests/test_prepare_db_aks_task.py
  api/tests/test_orphan_prepare_db_reconcile.py` → 29 passed, including two new
  tests: `test_on_job_progress_since_excludes_previous_snapshot_blobs` (stale
  blobs excluded, timestamp-less blob still counted) and
  `test_on_job_progress_without_since_counts_all_blobs` (reconciler path keeps
  the full count).
* Full suite `uv run pytest -q api/tests` → 2874 passed, 3 skipped.
* `uv run ruff check` (changed files) → clean.
* Confirmed live on `elb-cluster-02` that `16S ribosomal RNA` uses the AKS path
  (`prepare-db-16s-ribosomal-rna-260602010502` ConfigMap present), i.e. the path
  this fix targets.

## Deployment note

Baked into the `worker` image (the Celery task) — takes effect for newly
dispatched prepare-db Jobs after an `api` + `worker` image rebuild.
