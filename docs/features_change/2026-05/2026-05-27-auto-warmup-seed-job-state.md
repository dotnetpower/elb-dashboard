# Seed JobState before enqueueing auto-warmup tasks

## Motivation

App Insights end-to-end traces for the `api.tasks.storage.warmup_database`
task showed two `TableClient.get_entity` calls returning **404** at the
start of every auto-warmup-triggered run (`jobstate` table, RowKey
`current`, PartitionKey `auto-warmup-<cluster>-<db>-<ts>`). The 404s
surfaced as red Dependency failures on the dashboard and — more
importantly — silently dropped every phase checkpoint the worker tried
to write, so the SPA could never render progress for auto-warmup jobs.

Root cause: the `/warmup/start` route pre-creates the `JobState` row
before `send_task`, but
[`auto_warmup_reconcile.reconcile_auto_warmup_preferences`](../../../api/services/auto_warmup_reconcile.py)
enqueued the task with a fresh `job_id` without ever creating the row.
The worker's first `_update_state(job_id, "starting")` then ran
`repo.update()` → `get_entity()` → `ResourceNotFoundError` → caught as
`KeyError` → silent return, repeated for the second checkpoint.

## User-facing change

* Auto-warmup jobs now have a real `JobState` row from the moment the
  reconciler enqueues them, so the dashboard's warmup chips and
  `/api/tasks/{id}` status start reflecting `starting` / `downloading`
  / `sharding` / `warming` phases immediately instead of staying blank
  until the AKS-side warmup pods finished.
* App Insights stops showing the two phantom 404s per auto-warmup
  enqueue. The end-to-end transaction for `warmup_database` now only
  surfaces real failures.

## API / IaC diff summary

* `api/services/auto_warmup_reconcile.py`
  * New helper `_seed_auto_warmup_job_state(...)` builds a `JobState`
    (`type="warmup"`, `status="queued"`, `phase="queued"`,
    `owner_oid=pref.owner_oid`) with the canonical fields the warmup
    task and SPA expect and calls `state_repo.create()`.
  * New helper `_attach_auto_warmup_task_id(...)` calls
    `state_repo.update(job_id, task_id=...)` after `send_task` so
    `/api/tasks/{id}` and the SPA can resolve the Celery task from
    the job row.
  * The enqueue site inside `reconcile_auto_warmup_preferences` now
    builds `job_id` / `machine_type` / `num_nodes` / `program` once,
    seeds the JobState, sends the task, and attaches the resulting
    `task.id` — mirroring `/warmup/start`.
* No route or contract change; no IaC change.

## Validation evidence

* `uv run pytest -q api/tests/test_auto_warmup.py` — 12 passed
  (11 existing + 1 new
  `test_reconcile_auto_warmup_seeds_job_state_before_enqueue`).
* `uv run pytest -q api/tests/test_auto_warmup.py api/tests/test_warmup_route.py api/tests/test_warmup_database_readiness.py api/tests/test_warmup_jobs.py api/tests/test_state_repo.py api/tests/test_celery_failure_visibility.py`
  — 65 passed.
* `uv run pytest -q api/tests` — 1509 passed.
* `uv run ruff check api/services/auto_warmup_reconcile.py api/tests/test_auto_warmup.py` — clean.
