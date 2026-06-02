---
title: API concurrency and throughput improvements
description: Right-size the Celery worker, pool ARM management clients, parallelize node-pressure probes, tune the AnyIO threadpool, and isolate beat tasks on a dedicated reconcile queue.
tags:
  - architecture
  - infra
---

# API concurrency and throughput improvements

## Motivation

A deep review of the `api/` backend for concurrency correctness and parallel
throughput surfaced five findings. None are functional bugs, but together they
left the control plane over-subscribed under load and slower than necessary on
the polling monitor routes. All five are addressed here without changing any
externally observable contract.

The findings, in priority order:

1. **Worker over-subscription.** `run_celery_workers.py` spawns two prefork
   parents (worker-main concurrency 4 + worker-artifacts concurrency 2) = 6
   prefork children, but the worker sidecar was provisioned at only
   `0.5 vCPU / 1.0 Gi`. Under fan-out the children contended for half a core.
2. **ARM client churn.** Every monitor/resource call constructed a fresh
   `*ManagementClient`, each opening a new TLS session to
   `management.azure.com`. The polling dashboard paid repeated handshakes.
3. **Serial node-pressure probes.** `k8s/node_pressure.py` issued two
   independent Kubernetes API reads (nodes + pods) sequentially.
4. **Unbounded-by-default AnyIO threadpool.** The default 40-token limiter was
   never made explicit or tunable, so sync-in-async offload capacity could not
   be raised for I/O-bound bursts without code changes.
5. **Beat tasks sharing user queues.** Periodic reconcile tasks were routed onto
   the same `storage`/`blast`/`azure`/`default` queues as user-triggered work,
   so a backlog of scheduled scans could delay an interactive submit.

## User-facing change

No user-visible behaviour changes. This is a throughput/latency and
resource-headroom change:

* The monitoring dashboard refreshes faster (pooled ARM clients reuse TLS
  sessions; node-pressure reads run concurrently).
* Interactive BLAST/ACR/AKS actions are less likely to queue behind periodic
  reconcile tasks (dedicated `reconcile` queue).
* The worker sidecar has enough CPU/memory for its prefork children.

All new behaviour is opt-out / default-preserving via environment variables.

## API / IaC diff summary

* **Finding #1 — worker right-size + pool/memory knobs**
  * `infra/modules/containerAppControl.bicep`: worker sidecar
    `cpu 0.5 → 1.0`, `memory 1.0Gi → 2.0Gi`. New per-replica total
    `2.75 vCPU / 5.5 Gi` stays under the Consumption `4 vCPU / 8 Gi` cap
    (documented inline). **Requires redeploy / `az deployment group what-if`
    to take effect** — see Validation.
  * `api/run_celery_workers.py`: new `CELERY_POOL` (default `prefork`) and
    `CELERY_WORKER_MAX_MEMORY_PER_CHILD_KB` (default unset) env vars, validated
    by regex; `--pool` always emitted, `--max-memory-per-child` emitted only for
    prefork with a truthy non-zero value. `CELERY_MAIN_QUEUES` default gains
    `reconcile`. Prefork remains the default because the ARM pollers in
    `api/tasks/azure/*` rely on prefork signal-based `task_time_limit`; a threads
    pool would silently disable that backstop.
* **Finding #2 — ARM management-client pool**
  * `api/services/azure_clients.py`: added a process-wide pool keyed by
    `(kind, id(credential), subscription_id)` with a `threading.Lock`,
    `weakref.finalize`-based credential-GC eviction, and a `reset_mgmt_client_pool()`
    test hook. `resource/network/compute/storage/acr/aks/kv_mgmt` clients route
    through `_pooled_mgmt_client(...)`. Gated by `ENABLE_MGMT_CLIENT_POOL`
    (default `true`). `subscription/authorization/msi/kv_secret` clients are
    intentionally left unpooled.
  * `api/tests/conftest.py`: autouse fixture now calls `reset_mgmt_client_pool()`
    in setup and teardown so pooled clients never leak across tests.
* **Finding #3 — parallel node-pressure probe**
  * `api/services/k8s/node_pressure.py`: the two independent `session.get()`
    reads (nodes + pods) now run concurrently on a 2-worker
    `ThreadPoolExecutor`. Output and error handling unchanged.
* **Finding #4 — explicit, tunable AnyIO threadpool**
  * `api/app/lifespan.py`: `_configure_threadpool_capacity()` reads
    `API_THREADPOOL_TOKENS` (unset/invalid/non-positive → unchanged 40-token
    default) and sets the AnyIO default thread limiter at startup.
* **Finding #5 — dedicated beat reconcile queue**
  * `api/celery_app.py`: all `beat_schedule[*].options.queue` set to
    `reconcile`. `task_routes` unchanged, so user-triggered `.delay()` calls
    still route to `azure/blast/storage/default`. worker-main consumes
    `reconcile`, so no task is orphaned.

## Validation evidence

* `uv run ruff check api` → **All checks passed!**
* `uv run pytest -q api/tests/test_mgmt_client_pool.py api/tests/test_job_artifacts.py`
  → 16 passed (new pool tests + new worker-command tests).
* `uv run pytest -q api/tests/test_k8s_node_pressure.py api/tests/test_smoke.py api/tests/test_celery_failure_visibility.py`
  → 84 passed (node-pressure, smoke, celery consumers).
* Full suite `uv run pytest -q api/tests` → 2406 passed (one pre-existing xdist
  cross-file flaky failure, unrelated to these files — both candidates pass in
  isolation).
* New tests added: `api/tests/test_mgmt_client_pool.py` (pool reuse / per-key
  isolation / env opt-out / reset+close) and three `test_job_artifacts.py` cases
  covering the worker `--pool` / `--max-memory-per-child` contract.

### Redeploy note (Finding #1 only)

The worker CPU/memory bump is an `infra/` Bicep change. It only takes effect on
the next `postprovision.sh` / `az deployment group create` against
`containerAppControl.bicep`; the running revision is unaffected until then.
Validate with `az deployment group what-if` before applying. The other four
findings are pure `api/` code and need no redeploy.
