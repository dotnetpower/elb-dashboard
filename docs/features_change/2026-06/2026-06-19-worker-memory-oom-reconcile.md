---
title: Worker sidecar OOM fix + quick-deploy resource reconciliation
description: Raise the Celery worker sidecar to 1.0 vCPU / 2.0Gi with a per-child memory recycle cap, and make quick-deploy.sh reconcile container cpu/memory from the Bicep template so committed sizing changes stop silently drifting away from the live app.
tags:
  - operate
  - infra
---

# Worker sidecar OOM fix + quick-deploy resource reconciliation

## Motivation

A live App Insights / Log Analytics error scan of the deployed control plane
surfaced a recurring, ongoing failure: the Celery `worker` sidecar's prefork
child processes were being kernel **OOM-killed** (`signal 9 SIGKILL`,
`billiard.exceptions.WorkerLostError: Worker exited prematurely`). The rate was
~254 kills / 24 h (~1 every 4 minutes). The container itself never restarted
(`RestartCount = 0`) because Celery replaces a killed child with a fresh one, so
user-facing flows still completed (a live 16S BLAST submit→run→results finished
correctly during the investigation, and `AppRequests` 5xx in the run window was
zero) — but the churn wasted fork/boot cycles and was a latent reliability risk.

### Root causes

1. **Under-provisioned worker, sizing change never deployed.** The worker
   sidecar runs `run_celery_workers.py`, which spawns two Celery parents
   (`worker-main` @ concurrency 4 + `worker-artifacts` @ concurrency 2) = **6
   prefork children + 2 MainProcess** sharing the sidecar's memory cgroup. The
   Bicep template was bumped to `1.0 vCPU / 2.0Gi` on 2026-06-02 (commit
   `7fe304d`), but the **live app was still running `0.5 vCPU / 1.0Gi`** — the
   committed bump had never reached production.
2. **`quick-deploy.sh` preserves live resources.** Every fast / GitHub-Actions
   deploy PATCHes only the container image
   (`az containerapp update --container-name X --image …`), a read-modify-write
   that keeps the live `cpu`/`memory`. So a Bicep sizing change only lands on a
   full `azd provision`, which had not run since the bump — the worker silently
   ran under-provisioned for weeks.
3. **No per-child memory recycling.** `CELERY_WORKER_MAX_MEMORY_PER_CHILD_KB`
   was unset, so a child could grow unbounded until the shared limit was hit and
   the kernel killed it.

## User-facing change

No UI change. Operationally:

- The deployed `worker` sidecar now runs at **1.0 vCPU / 2.0Gi** (per-replica
  total `2.75 vCPU / 5.5Gi`, still within the Consumption `4 vCPU / 8Gi` cap and
  the `1 vCPU : 2 GiB` ratio) with a **per-child recycle cap of 250000 KiB
  (~244 MiB)**, so a leaking child is recycled after its task instead of
  OOM-killing the pool.
- A committed Bicep sizing change now **reaches the live app through a fast
  deploy**, not just a full `azd provision` — `quick-deploy.sh` reconciles each
  sidecar's `cpu`/`memory` from the Bicep template on every image PATCH.

## API / IaC diff summary

- [infra/modules/containerAppControl.bicep](../../../infra/modules/containerAppControl.bicep):
  added `CELERY_WORKER_MAX_MEMORY_PER_CHILD_KB=250000` to the worker env (the
  `1.0/2.0Gi` resources were already committed). `infra/main.json` recompiled to
  match.
- [scripts/dev/quick-deploy.sh](../../../scripts/dev/quick-deploy.sh): new
  `container_desired_resources()` helper parses the per-sidecar `cpu`/`memory`
  from the Bicep template (single source of truth); every `az containerapp
  update` image PATCH in both the `all` and single-sidecar paths now passes
  `--cpu`/`--memory` reconciled to that value. Best-effort: a parse failure
  falls back to an image-only PATCH (live resources preserved) with a warning,
  so a sizing reconcile can never block a code deploy. Each sidecar's Bicep
  value is individually `1 vCPU : 2 GiB`, so moving any one container to its
  Bicep value keeps the per-replica total a valid Container Apps combo.

## Validation evidence

- **Live apply**: `az containerapp update --container-name worker --cpu 1.0
  --memory 2.0Gi --set-env-vars CELERY_WORKER_MAX_MEMORY_PER_CHILD_KB=250000`
  rolled revision `ca-elb-dashboard--0000619` (`RunningAtMaxScale`, 1 replica);
  `worker-main`/`worker-artifacts` came up `ready` at concurrency 4 + 2 and
  tasks succeeded. **Zero `signal 9 SIGKILL` after the new revision started**
  (the remaining kills in the window all predate the revision swap, on the old
  `0000618`). Early signal — to be confirmed over a longer soak.
- **Parser**: extracts the correct `cpu memory` for every sidecar
  (`api 0.5 1.0Gi`, `worker 1.0 2.0Gi`, `beat 0.25 0.5Gi`, `frontend 0.25
  0.5Gi`, `terminal 0.5 1.0Gi`, `redis 0.25 0.5Gi`) and yields nothing for an
  unknown name.
- **Script**: `bash -n scripts/dev/quick-deploy.sh` clean; empty/full
  `--cpu/--memory` array splat verified safe under `set -Eeuo pipefail`.
- **Tests**: `uv run pytest -q api/tests -k "bicep or containerapp or
  control_plane or worker or resource or sidecar"` → 168 passed;
  `test_upgrade_escape_hatch.py` (references quick-deploy) → 4 passed;
  `test_control_plane_env.py` / `test_dockerfile_single_worker.py` /
  `test_redis_broker_eviction_policy.py` → 14 passed.
