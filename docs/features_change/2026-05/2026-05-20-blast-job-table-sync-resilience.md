# BLAST Job Table Sync — Resilience, Multi-User, Performance

## Motivation

Three classes of problems made the BLAST jobs list look inconsistent or stale:

1. **AKS-only jobs got lost on cluster recreation** — jobs submitted via the
   `elastic-blast` CLI inside AKS lived only in the external OpenAPI plane's
   ConfigMaps and never reached the dashboard's Table Storage.
2. **Celery failures left zombie rows** — if the broker was down when a user
   submitted, the Table row stayed `queued` forever. If a worker died mid-flight,
   the Table row stayed `running` until manually fixed.
3. **Delete button looked broken** — clicking Delete soft-deleted the row in
   Table, but the next external-OpenAPI poll resurrected the row because the
   merge step did not check the tombstone.

There were also performance and ownership concerns:

* Several UI components polled the same `/api/blast/jobs` endpoint concurrently;
  each call fired an upstream OpenAPI HTTP request from scratch.
* Sync looked up N rows in N round-trips against Azure Table Storage.
* In a multi-user deployment, the first caller to discover an external job
  claimed ownership and hid the row from every other caller with the same ARM
  scope.

## User-Facing Change

* **AKS-originated jobs persist on the dashboard.** First time the dashboard
  sees a job in the external OpenAPI plane, it copies the row into Table Storage
  with `owner_oid=""` (cluster-shared). Subsequent polls update the row's
  status/phase in place when the external plane has moved on.
* **Delete actually deletes.** Clicking the trash icon flips the row to a
  `deleted` tombstone that the list endpoint hides and that the next external
  sync respects (so the row stays gone forever, not just until the next poll).
* **Broker outage no longer leaves zombie rows.** If the Celery broker is
  unreachable at submit time, the row created moments earlier is immediately
  flipped to `failed / broker_unavailable`, and the API returns 503 so the
  dashboard surfaces a real error instead of a perpetual "queued" entry.
* **Worker-died rows get reconciled.** A new beat task scans all `queued /
  pending / running / reducing` rows every 60 s and brings them back to truth:
  Celery `FAILURE` / `REVOKED` → `failed`, `SUCCESS` → `completed`, otherwise
  asks the external plane, otherwise (silence past the stale threshold) marks
  the row `failed / phase=worker_lost / error_code=worker_lost`.
* **Multi-user environments work.** External rows are stored with
  `owner_oid=""` and `list_for_owner` now matches `(owner_oid eq <caller> or
  owner_oid eq '')` so every user with ARM scope on the cluster sees the same
  cluster-shared jobs. The dashboard's own submit path still writes the
  caller's OID, so per-user privacy of submitted jobs is unchanged.

## API / IaC Diff Summary

### Backend

* [api/services/state_repo.py](../../../api/services/state_repo.py)
  * `create()` handles `ResourceExistsError` by returning the existing row
    instead of raising, so concurrent sync calls are safe.
  * New `get_many(job_ids)` performs a single OData query across N PartitionKeys
    instead of N round-trips.
  * New `list_active(job_type='blast', limit)` returns rows in `queued /
    pending / running / reducing` for the reconciliation beat.
  * `list_for_owner()` now filters out tombstones and includes
    `owner_oid=""` rows.
* [api/routes/\_blast\_shared.py](../../../api/routes/_blast_shared.py)
  * `_sync_external_jobs_to_table()` now returns
    `(created, updated, tombstoned_ids)`:
    * Existing row with status drift → `update`.
    * Existing row with no drift → no-op (no `jobhistory` row per poll).
    * Existing tombstoned row → recorded in `tombstoned_ids` so the caller
      drops it from the response.
  * New 15 s in-memory cache for `external_blast.list_jobs(**kwargs)`
    (`_external_list_jobs_cached`) collapses several near-simultaneous polls
    into one upstream HTTP request.
  * New `_reset_external_jobs_cache()` test hook.
* [api/routes/blast.py](../../../api/routes/blast.py)
  * `/api/blast/jobs` now collects external candidates, runs the sync once,
    and uses the returned `tombstoned_ids` to skip tombstoned rows from the
    in-memory list (root cause of the "delete does nothing" bug).
  * `POST /api/blast/submit` catches the 503 from `_safe_delay` and flips the
    just-created row to `failed / phase=broker_unavailable /
    error_code=broker_unavailable` before re-raising.
* [api/tasks/blast.py](../../../api/tasks/blast.py)
  * New `reconcile_stale_jobs` task — scans active rows, consults Celery
    `AsyncResult`, falls back to the external plane, and marks long-silent
    rows `worker_lost`.
* [api/celery\_app.py](../../../api/celery_app.py)
  * Beat schedule wires `reconcile_stale_jobs` to run every 60 s on the
    `blast` queue.
* [api/conftest.py](../../../api/conftest.py)
  * Autouse fixture clears the external-jobs cache between tests so mocks
    cannot leak across cases.

### Frontend

* [web/src/pages/BlastJobs/useBlastJobsState.ts](../../../web/src/pages/BlastJobs/useBlastJobsState.ts)
  * Delete mutation now invalidates both `["blast-jobs", …]` and
    `["blast-jobs-for-pulse", …]` and drops the per-job detail cache.

No IaC changes.

## Validation Evidence

* `uv run pytest -q api/tests/` → **699 passed** (was 676 before this change).
* `uv run ruff check api/` → all checks passed.
* `cd web && npm run build` → succeeded (existing chunk-size warning only).
* End-to-end against live Azure Table Storage `elbstg01`:
  * Direct `DELETE /api/blast/jobs/<id>` → Table row flips to `status=deleted`.
  * Subsequent `GET /api/blast/jobs?...` → tombstoned row is hidden.
  * Repeated polls (with external OpenAPI returning the same row) → row stays
    hidden, no resurrection.
  * Browser sequence (Trash → Permanently delete) → dashboard count goes from
    6 to 5 to 4 jobs as deletes accumulate; no stale row reappears across
    reloads.
* Regression tests added:
  * `test_create_returns_existing_on_resource_exists`
  * `test_get_many_batches_into_single_query`
  * `test_list_active_filters_to_in_flight_states`
  * `test_list_for_owner_includes_cluster_shared_rows`
  * `test_sync_external_jobs_creates_missing_rows`
  * `test_sync_external_jobs_updates_drifted_status`
  * `test_sync_external_jobs_skips_unchanged_status`
  * `test_external_jobs_cache_serves_repeat_requests`
  * `test_sync_skips_tombstoned_deleted_rows`
  * `test_submit_marks_row_broker_unavailable_when_celery_down`
  * `test_reconcile_celery_failure_marks_row_failed`
  * `test_reconcile_celery_success_marks_row_completed`
  * `test_reconcile_skips_recently_updated_unknown_task`
  * `test_reconcile_marks_old_quiet_row_worker_lost`
