---
title: Orphaned prepare-db reconciler
description: Beat-scheduled reconciler that drives stuck AKS-fanout prepare-db markers to a terminal state when their driving Kubernetes Job is gone or failed.
tags:
  - blast
  - infra
---

# Orphaned prepare-db reconciler

## Motivation

When a database download is fanned out across an AKS Job, the `api`/`worker`
sidecar polls the Job and the per-file blobs and writes the terminal
`copy_status` (`completed` via `_promote_success`, or `partial` via
`_mark_partial`) **before** deleting the Job. If that poller process dies
mid-flight — e.g. a worker/beat revision redeploy — the `{db}-metadata.json`
row freezes with `update_in_progress: true` and a non-terminal
`copy_status.phase` (`queued`/`copying`), while the Job that was driving it is
already gone. The SPA then shows a perpetual download spinner and the
prepare-db in-progress gate keeps returning `409`, with no automatic recovery:
the only escape was the route's 2-hour stale window or a manual **Cancel**.

This happened in production for `nt` (frozen at `4810 / 4874` with no Job/pod)
and required an authenticated `cancel` + re-`prepare-db` to recover by hand.

## User-facing change

A new beat-scheduled reconciler now detects these orphaned markers and drives
them to a terminal `partial` phase automatically, so the spinner stops and the
gate clears without human intervention. The user finishes with a single
**Update** click, which idempotently skips already-staged files.

The reconciler is conservative by design:

- The **authoritative orphan signal is the Kubernetes Job lookup, not age.** A
  healthy multi-GB download legitimately exceeds the 2-hour stale window, so an
  age-only reset would abort live downloads. A row is reset only when its
  recorded `aks_job_ref` Job is **missing** or carries a **Failed** condition.
  An active/running/complete Job is left alone, and a transient Job-lookup
  failure is skipped (retried next tick) rather than guessed.
- Age is used **only as a fallback** when no `aks_job_ref` was recorded
  (server-side mode), using the same 2-hour threshold the route already uses.
- The reset **never re-dispatches** a download and **never auto-promotes /
  auto-shards** — it only flips the marker to `partial` + `update_in_progress:
  false`, records the staged-blob `success` count and `total_files`, and drops
  `aks_job_ref`. Honest state, no background download loops.
- The reset write is **ETag-guarded and re-validates `update_started_at`**, so
  if a fresh dispatch lands between the read and the write, the reconciler
  abandons the write instead of clobbering the new download (concurrency-race
  guard). The reconciler is idempotent — a reset row becomes terminal and is
  skipped on the next tick.

A kill-switch env `PREPARE_DB_ORPHAN_RECONCILE_ENABLED` (default `true`) and a
cadence env `CELERY_BEAT_PREPARE_DB_ORPHAN_SECONDS` (default `300`) are
available.

## API / IaC diff summary

- New service `api/services/storage/orphan_prepare_db.py`
  (`reconcile_orphaned_prepare_db`, `classify_prepare_db_entry`).
- New thin Celery task `api/tasks/storage/reconcile_orphan_prepare_db.py`
  (`api.tasks.storage.reconcile_orphaned_prepare_db`), re-exported from the
  `api.tasks.storage` package facade.
- New beat entry `prepare-db-orphan-reconcile` in `api/celery_app.py`
  (queue `reconcile`, default 300 s).
- No Bicep / Container App template change. **Activation requires a
  worker + beat image redeploy** (the task and beat schedule are baked into the
  worker/beat sidecars).

## Validation evidence

- `uv run pytest -q api/tests/test_orphan_prepare_db_reconcile.py` — 16 passed
  (classifier branches: missing/failed/running/complete job, lookup
  unavailable, no-ref recent/stale/unparseable, terminal phase, not in
  progress; orchestrator: disabled, no-storage-account, missing-job reset to
  partial, running-job untouched, lookup-exception skip, fresh-dispatch race
  skip).
- `uv run pytest -q api/tests/test_tasks_facade_contract.py
  api/tests/test_auto_warmup.py` — 73 passed (facade `__all__` contract +
  sibling reconciler unaffected).
- `uv run ruff check api/...` — clean.
- Celery registration confirmed: task `registered: True`,
  beat `prepare-db-orphan-reconcile: True`.
