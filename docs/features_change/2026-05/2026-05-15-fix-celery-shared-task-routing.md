# 2026-05-15 — Fix `shared_task.delay()` resolving to phantom Celery app (AKS provision silently dropped)

## Motivation
Clicking **Provision** in the SPA returned `200 OK` from `POST /api/aks/provision`,
but the AKS cluster never appeared. The Celery worker was healthy and registered
all tasks (including `api.tasks.azure.provision_aks`), yet nothing ever
moved from queue → worker.

## Root cause
The api sidecar imports `api.tasks.azure` lazily inside the route handler:

```python
@aks_router.post("/provision")
def aks_provision(...):
    from api.tasks.azure import provision_aks   # ← lazy, FIRST import
    result = _safe_delay(provision_aks, ...)    # → uses task.app (current_app)
```

`api.tasks.__init__` did **not** import `api.celery_app` first. So at the moment
the `@shared_task` decorators ran, no Celery app had been instantiated yet, and
`current_app` returned celery's internal phantom default app (`main='default'`,
`broker='amqp://'`, **`task_routes={}`**, **`task_default_queue='celery'`**).
`shared_task` bound the tasks to *that* app forever.

When `provision_aks.delay()` ran later:
- It used the phantom app's router → no route matched → fell through to its
  `task_default_queue='celery'`.
- The worker container subscribes only to `default,azure,blast,storage`.
- Messages stacked up in the `celery` Redis list and were never consumed.

A live diagnostic in production confirmed the split:
```
task_app_id    : 132600762876176          ← phantom default app
celery_app_id  : 132600762186832          ← our real app
current_app_id : 132600762876176          ← phantom default app
task_app_main          : "default"
task_app_routes        : {}
task_app_default_queue : "celery"
resolved_route         : {"queue": "celery"}
```

## User-facing change
- AKS provision now actually runs (verified end-to-end on `ca-elb-control--0000036`,
  `diag_noop` task: enqueue → SUCCESS in ~4 s).
- Same fix unblocks every other Celery-backed flow: ACR build, BLAST submit/cancel/status,
  storage warmup, scheduled `check_database_updates`.
- No SPA changes needed.

## Code change summary
- `api/celery_app.py` — explicit `set_as_current=True` + `set_default()` +
  `set_current()` after construction (belt-and-braces against any other module
  instantiating a Celery app first).
- `api/tasks/__init__.py` — load `api.celery_app` **before** `acr/azure/blast/storage`
  (load-bearing import order, with comment).
- `api/main.py` — eager `from api import celery_app as _celery_app` so the api
  sidecar's uvicorn workers each have our Celery instance registered as
  `current_app` before any request handler runs.
- `api/tasks/azure.py` — added `diag_noop` Celery task for permanent enqueue↔consume
  round-trip diagnostics.
- `api/routes/health.py` — added unauthenticated diagnostic endpoints
  (`GET /api/health/celery`, `POST /api/health/celery/enqueue-noop`,
  `GET /api/health/celery/result/{task_id}`) so future drift surfaces immediately
  instead of "POST 200 + cluster never appears".

## Validation evidence
- `uv run pytest -q api/tests` → **67 passed** (was 56; +diag tests).
- Local repro with `redis:7-alpine` on `127.0.0.1:16379`:
  ```
  task.app id    : 139768660866784
  celery_app id  : 139768660866784
  current_app id : 139768660866784
  task.app.main  : elb_control_plane
  task.app.routes: {'api.tasks.azure.*': {'queue': 'azure'}, ...}
  ```
- Production `ca-elb-control--0000036` after deploy:
  ```
  POST /api/health/celery/enqueue-noop?message=after-fix → 200
    task_app_id == celery_app_id == current_app_id
    resolved_route.queue = "azure"
  GET /api/health/celery/result/<task_id>  (after 4 s)
    status: SUCCESS
    result.message: "after-fix"
  GET /api/health/celery
    queues: {default:0, azure:0, blast:0, storage:0, celery:0}
    redis_keys_db0: []   ← no orphaned messages
  ```

## Out of scope
- The legacy messages already sitting in the `celery` Redis list from before the
  fix are dropped on the next revision swap (in-memory Redis sidecar, no AOF).
  Any user who clicked Provision before this deploy must click again.
- The diagnostic endpoints under `/api/health/celery/*` are unauthenticated by
  design (read-only, no secrets) — same posture as `/api/health` and
  `/api/health/ready`.
