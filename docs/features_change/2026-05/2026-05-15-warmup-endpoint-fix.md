# 2026-05-15 — Wire DB Warmup button to the Celery task

## Motivation

The "Start warmup" button in the AKS cluster detail modal was effectively a no-op:

1. **Response shape mismatch.** `POST /api/warmup/start` returned `{"id": ...,
   "task_id": ...}`, but the SPA's `WarmupSection` (carried over from the
   Durable Functions era) reads `resp.instance_id`. That meant
   `setWarmupInstanceId(undefined)` and the literal string `"undefined"`
   was persisted to `localStorage` under `elb-warmup-<cluster>`.
2. **Body field name mismatch.** The SPA sends the DB selection as
   `db: "blast-db/16S_ribosomal_RNA"` plus `db_display_name`, but the
   route only read `body.get("database_name", "")`. The Celery task
   therefore received an empty database name and immediately failed
   with `unknown database: ` (visible in the worker log:
   `succeeded in 0.002s: {'status': 'failed', 'error': 'unknown database: '}`).
3. **Status endpoint was a stub.** `GET /api/warmup/{instance_id}/status`
   always returned `runtime_status: "Pending"` with `degraded: true`,
   regardless of the underlying Celery task. The SPA polled it forever
   and never observed Completed/Failed.

## User-facing change

- "Start warmup" in the AKS cluster detail modal now actually submits the
  Celery `warmup_database` task and the SPA can poll its real status.
- The "Cached on nodes" section finishes correctly: when the Celery
  task succeeds, the orchestrator-style polling reports `Completed`
  with `output.status = "succeeded"` and the SPA refreshes the
  warmup-status query, lighting up the corresponding badge.
- Stale `localStorage` entries with `instance_id="undefined"` are
  self-healed by `WarmupSection`'s existing `orchQuery.isError`
  cleanup branch.

## API/IaC diff summary

`api/routes/stubs.py`:

- New helper `_resolve_warmup_db_name(body)` accepts either the new SPA
  shape (`db` / `db_display_name`) or the legacy `database_name` key
  and strips a `blast-db/` container prefix when present.
- `POST /api/warmup/start` now passes the resolved DB name to
  `warmup_database` and returns `{ id, instance_id, task_id, db,
  statusQueryGetUri, status }`. `instance_id == task_id == Celery
  AsyncResult id`. `id` (the JobStateRepository row id) is kept for
  back-compat.
- `GET /api/warmup/{instance_id}/status` is no longer a stub. It looks
  up the Celery `AsyncResult` and translates the state to the
  Durable-Functions–style payload the SPA already understands:
  - `PENDING/RECEIVED → Pending`
  - `STARTED/RETRY/PROGRESS → Running` (passes through `result.info`
    as `custom_status`)
  - `SUCCESS → Completed` with `output: { status: "succeeded" |
    "failed", db, error? }`
  - `FAILURE → Failed`, `REVOKED → Terminated`

No infrastructure changes. No new dependencies.

## Validation evidence

```
$ uv run pytest -q api/tests
123 passed in 10.85s
```

```
# 1) Start warmup — response now carries instance_id and the resolved db
$ curl -s -X POST http://127.0.0.1:8080/api/warmup/start \
    -H 'Authorization: Bearer __dev_bypass__' \
    -H 'Content-Type: application/json' \
    -d '{"subscription_id":"sub","resource_group":"rg-elb-01",
         "storage_account":"elbstg01",
         "db":"blast-db/16S_ribosomal_RNA",
         "db_display_name":"16S_ribosomal_RNA",
         "aks_cluster_name":"elb-cluster"}' | python3 -m json.tool
{
    "id": "e96205e7-8171-4ee0-97a9-bc14b7ea071e",
    "instance_id": "4834de46-5c58-465b-a6cb-c9e45f03fbf2",
    "task_id":     "4834de46-5c58-465b-a6cb-c9e45f03fbf2",
    "db": "16S_ribosomal_RNA",
    "statusQueryGetUri": "/api/tasks/4834de46-5c58-465b-a6cb-c9e45f03fbf2",
    "status": "queued"
}

# 2) Poll status — real Celery AsyncResult, no longer a stub
$ curl -s http://127.0.0.1:8080/api/warmup/4834de46-5c58-465b-a6cb-c9e45f03fbf2/status \
    -H 'Authorization: Bearer __dev_bypass__' | python3 -m json.tool
{
    "instance_id": "4834de46-5c58-465b-a6cb-c9e45f03fbf2",
    "runtime_status": "Completed",
    "custom_status": { "phase": "completed", "db": "16S_ribosomal_RNA" },
    "output":        { "status": "succeeded", "db": "16S_ribosomal_RNA" }
}
```

## Cross-repo consistency

None — this is purely a control-plane wiring fix. The sibling
`elastic-blast-azure` repo is untouched.
