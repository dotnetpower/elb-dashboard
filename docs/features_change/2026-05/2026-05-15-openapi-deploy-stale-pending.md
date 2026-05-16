# 2026-05-15 — Fix permanent "Deploying…" on /docs (broker split-brain + stale-PENDING guard)

## Motivation

User reported the OpenAPI deploy task on `/docs` was stuck in `pending
(1758s)` indefinitely. Two compounding issues:

1. **Broker split-brain.** The host-side `api` (`uvicorn` started by
   `scripts/dev/local-run.sh api`) talks to `redis://127.0.0.1:6379/0`,
   which is `elb-dev-redis`. The docker-compose `worker` container
   (`elb-control-local-worker-1`) talks to `redis://redis:6379/0` inside
   its own compose network, which is `elb-control-local-redis-1`. They
   are different Redis instances. Tasks enqueued from the api therefore
   went to a queue no worker was consuming → permanent PENDING.
2. **No stale-PENDING recovery in the SPA.** Celery's `AsyncResult.status`
   returns `PENDING` both for "task hasn't started" and "task id unknown
   to the result backend". The status endpoint maps that to
   `runtime_status: "Pending"` and the SPA's `OpenApiDeployPanel` polls
   forever without ever reaching the `isError` branch that would clear
   localStorage. So even after the broker was fixed, the panel kept
   chasing the dead task id stored in localStorage.

## User-facing change

- The `/docs` deploy panel now self-recovers from a "ghost" task: after
  5 minutes of unbroken `runtime_status: "Pending"` it surfaces an error
  ("OpenAPI deploy never started (the worker may not be running). Click
  Deploy to retry.") and clears the persisted task id.
- A new **Cancel** button appears next to **Retry Discovery** whenever a
  deploy task is being tracked. It clears the SPA's local state without
  attempting to abort the underlying Celery task — useful when the user
  knows the task is dead and wants to deploy again immediately.

## API / IaC diff summary

- `web/src/components/OpenApiDeployPanel.tsx`
  - Added `STALE_PENDING_TIMEOUT_MS = 300_000` and a guarding `useEffect`
    that fires when `runtime_status === "Pending"` AND
    `now - deployStartedAt >= STALE_PENDING_TIMEOUT_MS`.
  - Added `handleCancelTracking` + a **Cancel** button rendered when
    `deployInstanceId && !deploySucceeded`.
  - Imported `X` from `lucide-react`.
- No backend or infra changes.

## Operational note (broker)

Local development must use **one** Redis. Either:

- Start the worker via `scripts/dev/local-run.sh worker` (preferred —
  matches AGENTS.md and shares `127.0.0.1:6379` with the host api), or
- Run the api inside `compose-local` so it hits `redis://redis:6379` too.

Mixing host-`api` with compose-`worker` will silently drop every task.
This was the root cause of today's hang. The new stale-PENDING guard
makes the symptom visible (5-minute error) instead of trapping the user
in an infinite spinner, but the underlying configuration mistake should
still be avoided.

## Validation evidence

- Started `scripts/dev/local-run.sh worker` on the host. Worker logs
  show `Connected to redis://127.0.0.1:6379/0` and registers
  `api.tasks.openapi.deploy_openapi_service`.
- `curl POST /api/aks/openapi/deploy …` → returned task id; subsequent
  `GET …/status` polls transitioned `Pending → Running (phase:
  setup_workload_identity, cluster_name: elb-cluster)` within ~6
  seconds. Task is no longer ghosting.
- Browser screenshot of `/docs` after clearing the stale localStorage
  entry shows the **Deploy elb-openapi** + **Retry Discovery** buttons
  enabled (and after this PR, **Cancel** appears once a task is
  tracked).
- Frontend type-check: no new TS errors in `OpenApiDeployPanel.tsx`.
