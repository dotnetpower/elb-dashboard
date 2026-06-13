---
title: Stale warmup / prepare_db jobstate rows now reach a terminal status
description: A dedicated reconciler terminalises crashed-worker dbops/warmup rows, synchronous audit rows are born terminal, and auto-stop recognises the prepare_db sub-types.
tags:
  - operate
  - blast
---

# Stale warmup / prepare_db jobstate rows reach a terminal status (#34)

## Motivation

`warmup` and `prepare_db_*` jobstate rows could leak in an active status
(`queued` / `running`) **forever** when their owning work died without writing
a terminal status:

- `api.tasks.blast.reconcile_stale_jobs` only scans `job_type="blast"`.
- `reconcile_orphaned_prepare_db` reconciles **Storage `{db}-metadata.json`**,
  never the jobstate Table rows.
- Synchronous DB-admin ops (`prepare_db_cancel` / `prepare_db_delete`) recorded
  their audit row as `queued` even though the op had already finished inside the
  request — there was no later writer to terminalise them.

Live evidence (2026-06-13, `elb-cluster-01`): 1 stuck `warmup` (running ~10 h),
15 `prepare_db_aks`, 10 `prepare_db_cancel`, 2 `prepare_db_delete`, 2
`prepare_db` — all `queued`/`running` for days while their K8s Jobs had
completed hours earlier.

## User-facing change

- Stale dbops/warmup rows no longer linger as phantom active work in the job
  list / audit log; they are driven to `completed` or `failed` (worker_lost).
- An in-flight AKS-fanout download (`prepare_db_aks`) now correctly keeps an
  otherwise-idle AKS cluster alive (auto-stop prefix match), instead of being
  ignored because its type did not exactly equal `prepare_db`.

## Implementation

1. **Source fix** — `api.services.db.ops_audit.record_db_op` gained optional
   `status` / `phase` params (default `queued`). The synchronous
   `prepare_db_cancel` / `prepare_db_delete` routes now record `status="completed"`
   so their audit rows are born terminal. The appended history event mirrors the
   status (`completed` vs `started`).
2. **New reconciler** — `api.services.db.stale_dbops` (`classify_dbops_row` pure
   decision + `reconcile_dbops` orchestrator) wrapped by the Celery task
   `api.tasks.storage.reconcile_stale_dbops_jobs`, beat-scheduled every 300 s on
   the `reconcile` queue. Per-type policy:
   - synchronous ops (`prepare_db_cancel` / `prepare_db_delete`) → `completed`
     regardless of age (mops up pre-existing rows);
   - async ops (`warmup` / `prepare_db` / `prepare_db_aks` / `shard` / `oracle`)
     → `completed` on Celery `SUCCESS`, `failed`/`task_failed` on
     `FAILURE`/`REVOKED`, and `failed`/`worker_lost` only after a generous
     per-type quiet threshold (`STALE_DBOPS_WARMUP_SECONDS=7200`,
     `STALE_DBOPS_PREPARE_DB_SECONDS=21600`) with no live Celery record. The
     prepare-db threshold (6 h) comfortably exceeds the `prepare_db_aks` task
     hard time limit (~4 h 45 m) so a live download is never aged out.
   - Kill-switch `STALE_DBOPS_RECONCILE_ENABLED` (default on).
   - **Accepted simplification:** a Celery `SUCCESS` always maps the audit row
     to `completed`; a partial download's true state lives in
     `{db}-metadata.json` / the Storage card, not this coarse audit row.
3. **Auto-stop prefix match** — `api.services.auto_stop_evaluator` adds
   `_row_type_blocks_autostop`, which matches `ACTIVE_JOB_TYPES` exactly plus the
   `prepare_db` prefix family. The `_ACTIVE_ROW_STALE_SECONDS` zombie cap still
   backstops a crashed `prepare_db_aks` row at 2 h.

## API / IaC diff summary

- No API surface change. New env knobs (all optional, safe defaults):
  `STALE_DBOPS_RECONCILE_ENABLED`, `STALE_DBOPS_WARMUP_SECONDS`,
  `STALE_DBOPS_PREPARE_DB_SECONDS`, `CELERY_BEAT_STALE_DBOPS_SECONDS`.
- New beat entry `stale-dbops-reconcile` in `api/celery_app.py`.

## Validation evidence

- `uv run pytest -q api/tests/test_stale_dbops_reconcile.py` — 15 passed
  (pure-decision matrix, IO glue, orchestrator tally, kill-switch).
- `uv run pytest -q api/tests/test_db_ops_audit_status.py` — sync/async status
  contract.
- `uv run pytest -q api/tests/test_auto_stop_evaluator.py` — prefix-match guards
  + existing zombie age-out unchanged.
- Consumer sweep (146 tests across `test_prepare_db_routes`,
  `test_prepare_db_aks_route`, `test_peering_nsg_audit`,
  `test_storage_shared_taxonomy`, `test_settings_vnet_peering`,
  `test_auto_warmup`, `test_warmup_route`, `test_azure_tasks`,
  `test_tasks_facade_contract`) — green.
- `uv run ruff check` clean on every touched path.
- Note: the full `pytest -q api/tests` run trips a **pre-existing, unrelated**
  cross-test-pollution hang in `blast_job_get` → `external_blast.get_job` (a real
  httpx call against a leaked external OpenAPI base URL that waits out its
  timeout). That path is not touched by this change; `test_blast_jobs_routes.py`
  passes in isolation (15/15).

