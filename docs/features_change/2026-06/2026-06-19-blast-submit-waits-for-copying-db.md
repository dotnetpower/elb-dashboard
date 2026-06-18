---
title: BLAST submit waits for a still-copying database instead of failing
description: The submit task now performs a readiness-aware DB check and re-enqueues (waiting_for_database) while the prepare-db copy or a version update is in flight, closing the gap where a submit accepted before the DB finished warming — notably the OpenAPI path — would BLAST against incomplete volumes or fail outright.
tags:
  - blast
  - operate
---

# BLAST submit waits for a still-copying database instead of failing

## Motivation

A live functional review surfaced two related problems with API/OpenAPI-submitted
BLAST jobs:

1. The dashboard submit route gates on `validate_blast_database_ready` (which
   verifies `copy_status.phase == "completed"`), but the **submit task itself**
   only called `validate_blast_database_available` — a marker-blob existence
   check. A database that has marker blobs but is **still being copied** by the
   prepare-db pipeline therefore passed the task's check, so a job could be
   submitted to `elastic-blast` against incomplete volumes.
2. The **OpenAPI submit path** (`/api/v1/elastic-blast/submit`) has no
   submit-time readiness gate at all (the dashboard path does), so an external
   caller submitting before the DB finished warming had nothing protecting it
   until the task — which then failed immediately with no retry.

The two paths converge on the same Celery `submit` task, so the fix belongs
there.

## User-facing change

When a submit reaches the task while the database is in a **transient** state
— `database_not_ready` (prepare-db copy still running) or `database_updating`
(a version update is mid-flight) — the job no longer fails. It re-enqueues on a
new `waiting_for_database` phase (rendered as a calm Pending state, warning
colour) and waits for the copy/update to settle, then runs — exactly the proven
pattern the `waiting_for_warmup` loop already uses. A **permanent** error
(missing DB, invalid reference, persistent Storage failure) still fails fast as
`database_unavailable`, unchanged.

The wait loop is bounded by `BLAST_DATABASE_MAX_WAIT_SECONDS` (default 45 min,
matching the warmup deadline); a copy that never completes eventually fails with
a real terminal state instead of queueing forever. The re-enqueue keeps
`status="running"` so the reconciler treats the waiting row as active (a
`"queued"` result would be mis-reconciled to `completed`).

## API / IaC diff summary

No HTTP contract change. Internal:

- `api/tasks/blast/submit_task.py` — DB check upgraded from
  `_validate_blast_database_available` to `_validate_blast_database_ready`;
  transient codes (`database_not_ready` / `database_updating`) re-enqueue on
  `waiting_for_database` with a `database_wait_deadline_ts` (new optional,
  internal-only task kwarg) bounded by `_database_max_wait_seconds()`. Permanent
  codes fail fast as before.
- `api/tasks/blast/config_shims.py` + `api/tasks/blast/__init__.py` — new
  `_validate_blast_database_ready` shim, re-exported on `api.tasks.blast`.
- `web/src/constants.ts` + `web/src/components/cards/ClusterBento/jobMapping.ts`
  — register `waiting_for_database` (warning colour, Pending display state),
  mirroring `waiting_for_warmup`.

## Validation evidence

- `uv run ruff check api` — clean.
- `uv run pytest -q api/tests` — **4011 passed, 3 skipped**. New
  `api/tests/test_blast_submit_database_retry.py` (6 tests + parametrize) covers
  transient re-enqueue, deadline stamp/forward, deadline-exceeded fail, permanent
  fail-fast, broker-down fallback, and the reconciler keep-active contract.
  Updated `test_blast_database_availability.py`,
  `test_blast_submit_warmup_retry.py`, `test_blast_submit_capacity_gate.py`,
  `test_blast_tasks.py` to stub/expect the readiness shim.
- `cd web && npm run build` — succeeds; `jobMapping.test.ts` + `constants.test.ts`
  18 passed.

## Follow-ups (not in this change)

- "Indeterminate vs failed" UX: when the sibling OpenAPI plane is degraded /
  times out, a job's real status cannot be resolved and is shown opaquely
  ("OpenAPI service reported no error detail"). Surfacing a distinct
  status-unavailable/retry state (instead of a bare failure) is tracked
  separately.
- The 14-job QUEUED backlog observed live (10 min drain dwell + ~1h capacity
  wait) is an operational/capacity matter tied to the Service Bus drain and AKS
  concurrency (issue #52), not this code path.
