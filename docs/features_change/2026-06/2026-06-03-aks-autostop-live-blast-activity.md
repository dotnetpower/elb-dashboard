---
title: AKS auto-stop recognises live OpenAPI BLAST runs
description: Inject live K8s app=blast activity into the idle evaluator so OpenAPI-submitted runs keep a cluster alive, restore the countdown, and stop the start-state chip from flapping.
tags:
  - blast
  - operate
---

# AKS auto-stop recognises live OpenAPI BLAST runs

## Motivation

Three linked defects in the AKS idle auto-stop loop:

1. **Countdown missing before stop.** The "time until auto-stop" never
   showed; the cluster jumped straight to stopped.
2. **State flapping.** After a manual start, the header chip flashed
   `Starting...` → `Stopped` → `Starting...` on refresh.
3. **OpenAPI runs invisible.** A BLAST job submitted through the OpenAPI
   surface (not the dashboard) was never written to the dashboard
   `jobstate` Table, so the idle evaluator saw zero active jobs and
   stopped the cluster mid-run.

Root cause of (1) and (3) is the same: the evaluator only scanned the
dashboard jobstate Table (`_scan_cluster_jobs`). Without a live signal,
an active OpenAPI run looked idle → the idle clock had already expired →
no warn window (no countdown) → immediate stop.

## User-facing change

- A cluster running **any** `app=blast` workload — including OpenAPI-
  submitted runs the dashboard never recorded — is no longer auto-stopped.
  The auto-stop banner shows `active_jobs:N` with the real live count.
- The countdown is restored: live activity re-anchors the idle clock, so
  the warn window (and visible countdown) appears before a stop.
- The start/stop chip (`Starting...` / `Stopping...`) now persists until
  ARM `power_state` actually reaches the target, instead of clearing on
  Celery task success while `power_state` is still eventually-consistent —
  removing the flapping.

The live signal is **additive only**: it can keep a cluster alive or reset
the idle clock, but it can never force a stop. If the Kubernetes API is
unreachable the probe degrades to "no signal" and the evaluator falls back
to the existing jobstate-Table decision, so a K8s outage can never strand a
cluster running forever.

## API / code diff summary

- **NEW** `api/services/auto_stop_live.py` — `probe_live_blast_activity(pref)`
  best-effort read-only probe over `k8s_check_blast_status(job_id=None)`.
  Returns `(live_active_jobs, live_latest_activity)` or `None` on any
  failure (exception / non-dict / `status == "unknown"`). `completed` /
  `failed` runs with lingering pods report `active == 0` (do not block a
  stop) but still seed the activity anchor.
- `api/services/auto_stop_evaluator.py` — `evaluate_cluster` gains two
  optional params `live_active_jobs` / `live_latest_activity` (default
  `None`, backward compatible). Live active jobs are **added** to the
  jobstate count; live latest activity extends the idle anchor. The pure
  evaluator still performs no SDK calls.
- `api/tasks/azure/idle_autostop.py` — new `_live_blast_signal(pref,
  power_state)` helper (probes only when `power_state == "Running"`,
  degrades to `(None, None)`); wired into both the per-cluster
  `auto_stop_aks` re-evaluation and the beat `evaluate_idle_clusters`
  fan-out.
- `api/routes/aks/autostop.py` — `_compute_status` mirrors the same live
  probe (Running clusters only, try/except guarded) so the SPA status
  countdown agrees with the beat driver.
- `web/src/components/cards/ClusterCard/useClusterActions.ts` — keep the
  optimistic transition chip for `starting` / `stopping` until the real
  `power_state` settles; only `deleting` clears immediately.

No IaC change. No new dependency. The `active_jobs:{n}` reason string and
`active_job_count` field (SPA banner contract) are preserved.

## Validation

- `uv run pytest -q api/tests/test_auto_stop_live.py` — 9 passed (new).
- `uv run pytest -q api/tests/test_auto_stop_evaluator.py` — includes 6 new
  live-signal cases (add-not-max, fallback on `None`, anchor reset, stale
  anchor does not block).
- `uv run pytest -q api/tests/test_auto_stop_task.py` — 13 passed, including
  `test_auto_stop_aks_live_activity_blocks_idle_stop` and
  `test_live_blast_signal_skips_probe_when_not_running`.
- `uv run pytest -q api/tests/test_aks_autostop_route.py` — 20 passed.
- `uv run ruff check api` — clean.
- `cd web && npm run build` — succeeds; `npm test -- --run` — 536 passed.
