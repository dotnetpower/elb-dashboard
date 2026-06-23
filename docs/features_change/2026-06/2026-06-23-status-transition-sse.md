---
title: Push job status transitions to the SSE stream (live status updates)
description: Broadcast jobs-changed when publish_transitions advances a bridge (Queued→Running→Succeeded/Failed) and when reconcile_stale_jobs makes progress, so the dashboard status updates the instant the backend detects the change instead of waiting out a frontend poll.
tags:
  - blast
  - architecture
---

# Push job status transitions to the SSE stream

## Motivation

The jobs-events SSE (commit `8fb76a7`) pushes a `jobs-changed` event so the
dashboard refetches instantly — but only the **submit/drain** paths fired the
broadcast. A job's **status transitions** (Queued → Running → Succeeded/Failed)
are detected later, by `publish_transitions` (Service Bus bridge polling) and
`reconcile_stale_jobs` (k8s/Celery/external reconcile), and those paths did NOT
broadcast. So a status change surfaced only on the next dashboard poll — the
"status changes late" lag an operator observes while a queue-submitted job runs.

## User-facing change

* When `publish_transitions` publishes a real transition (a bridge advanced to a
  new status), it now fires the cross-sidecar invalidation → the jobs-events SSE
  pushes `jobs-changed` → the dashboard's Jobs list / Message Flow / job detail
  update the status live. No-op on an idle tick (nothing changed).
* When `reconcile_stale_jobs` makes progress (a row completed / failed /
  worker-lost / k8s or external refresh), it fires the same invalidation so a
  status it reconciles is pushed live too.
* The latency for a status change to reach the browser drops from
  *(backend-detect interval + frontend-poll interval)* to just the
  backend-detect interval — the frontend no longer adds its own poll delay.

## API / IaC diff summary

* `api/tasks/servicebus/tasks.py`: `publish_transitions` calls
  `_publish_jobs_cache_invalidate("servicebus_transition")` when `published` or
  `finished` > 0.
* `api/tasks/blast/reconcile_task.py`: `reconcile_stale_jobs` calls
  `publish_jobs_cache_invalidate("blast_reconcile_progress")` when
  `progress_made`.
* Both are best-effort and only fire when there is a real change to announce.
* No IaC change. No new dependency.

## Validation evidence

* `uv run ruff check api/tasks/servicebus/tasks.py api/tasks/blast/reconcile_task.py` — clean.
* `uv run pytest -q api/tests/test_servicebus_tasks.py -k publish_transitions_emits_on_change`
  — passes (a transition invalidates with `servicebus_transition`; the no-change
  second tick does not).
* `uv run pytest -q api/tests/test_blast_tasks.py -k reconcile_celery_success_marks_row_completed`
  — passes (a completed reconcile invalidates with `blast_reconcile_progress`).

## Note

This is the backend half. The browser must also be running the `useJobsEvents`
hook (commit `8fb76a7`, frontend) to consume the stream — deploy the frontend
sidecar alongside the api so the live-status path is end-to-end.
