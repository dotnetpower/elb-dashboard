---
title: Fix worker crash-loop that failed and slowed BLAST jobs
description: Disable Azure Monitor Live Metrics on the Celery prefork worker/beat sidecars and raise the billiard proc-alive timeout so prefork children stop being SIGKILL'd at boot, which was failing in-flight BLAST submits with WorkerLostError.
tags:
  - blast
  - operate
  - reliability
---

# Fix worker crash-loop that failed and slowed BLAST jobs

## Motivation

Most jobs on `/blast/jobs` were failing and even the successful ones were slow.

Investigation (App Insights `appi-elb-dashboard` + worker console logs on
`ca-elb-dashboard`) found the `elb-worker` Celery prefork pool in a permanent
**crash loop**:

- `ForkPoolWorker` indices had climbed past **8200** (thousands of children
  spawned and killed).
- The master repeatedly logged `Timed out waiting for UP message from
  <ForkProcess(...)>` immediately followed by `Process '...' exited with
  'signal 9 (SIGKILL)'` and `WorkerLostError: Worker exited prematurely:
  signal 9 (SIGKILL)`.
- `traces` showed `azure monitor opentelemetry initialised for role=worker`
  ~**349 times in one hour** (≈ one child re-init every ~10 s).

Root cause chain:

1. `init_telemetry` runs inside **every** prefork child via the
   `worker_process_init` Celery signal.
2. It called `configure_azure_monitor(enable_live_metrics=True)`. Live Metrics
   (QuickPulse) opens a dedicated streaming exporter + thread **per child** —
   one stream for each of the 6 prefork children (main concurrency 4 + artifact 2).
3. On the worker sidecar's 0.5 vCPU budget, that per-child boot work exceeded
   billiard's `worker_proc_alive_timeout` (~4 s default), so the master
   declared each child lost, `SIGKILL`'d it, and forked a replacement — forever.
4. With `task_acks_late=True` + `task_reject_on_worker_lost=True`, any BLAST
   task a dying child was holding was failed with `WorkerLostError` (or
   requeued and bounced between crashing children) — exactly the "most jobs
   fail, the rest are slow" symptom.

## User-facing change

- BLAST submit/monitor tasks now run on a stable worker pool. Jobs stop failing
  with `WorkerLostError` and complete without the crash-loop latency.

## API/IaC diff summary

- `api/app/telemetry.py`: new `_resolve_live_metrics_enabled(role)` — Live
  Metrics defaults **ON for `api`** (single uvicorn process, cheap + useful) and
  **OFF for `worker`/`beat`** (prefork, one QuickPulse stream per child is the
  boot cost). Overridable with `AZURE_MONITOR_DISABLE_LIVE_METRICS` /
  `AZURE_MONITOR_ENABLE_LIVE_METRICS`.
- `api/celery_app.py`: set `worker_proc_alive_timeout` (env
  `CELERY_WORKER_PROC_ALIVE_TIMEOUT`, default raised to **30 s**) so a slightly
  slow child boot on 0.5 vCPU is not SIGKILL'd.
- `infra/modules/containerAppControl.bicep`: add
  `AZURE_MONITOR_DISABLE_LIVE_METRICS=true` to the `worker` and `beat`
  containers so the fix persists across `azd provision` even before the new
  image ships.

## Validation evidence

- Immediate live mitigation (no image rebuild — deployed image already honoured
  the env var): `az containerapp update --container-name worker --set-env-vars
  AZURE_MONITOR_DISABLE_LIVE_METRICS=true` produced revision
  `ca-elb-dashboard--0000028` (`RunningAtMaxScale`).
- Post-fix worker console: SIGKILL / "Timed out waiting for UP message" count =
  **0**; `ForkPoolWorker` indices reset to a stable **1–4**; tasks
  (`servicebus.drain_and_resubmit`, `publish_transitions`, `reconcile_stale_jobs`)
  succeed.
- `uv run ruff check api/app/telemetry.py api/celery_app.py api/tests/test_telemetry_init.py`: passed.
- `uv run pytest -q api/tests/test_telemetry_init.py`: 12 passed (incl. new
  worker default-off + explicit-enable-override tests).
- `uv run pytest -q api/tests/test_telemetry_init.py api/tests/test_blast_tasks.py`: 157 passed.
- `az bicep build --file infra/modules/containerAppControl.bicep`: clean.
