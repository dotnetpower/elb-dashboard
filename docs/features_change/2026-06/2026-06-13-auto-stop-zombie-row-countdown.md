---
title: Auto-stop survives zombie jobstate rows; panel always explains itself
description: A crashed-worker jobstate row stuck in an active status no longer pins an AKS cluster alive forever or hides the auto-stop countdown / Extend controls.
tags:
  - blast
  - operate
---

# Auto-stop zombie-row age-out + always-explained panel (2026-06-13)

## Motivation

On the live deployment a cluster (`elb-cluster-01`) had auto-stop **enabled**
yet the dashboard showed **no remaining-time countdown and no Extend button**,
and the cluster never auto-stopped despite being idle for ~10 hours.

Root cause (confirmed against live `/api/aks/autostop/status` + the jobstate
Table): a single `warmup` jobstate row was stuck in `status="running"`
(`phase=warming_nodes`, `updated_at` frozen at `00:12`) because the warmup task
crashed without writing a terminal status. The auto-stop evaluator counts any
row whose `type ∈ {blast, warmup, prepare_db, shard, oracle}` and
`status ∈ {queued, pending, running, reducing}` as "cluster in use", with **no
age limit**. So the zombie row:

1. reported `verdict=keep, reason=active_jobs:1` forever → the cluster was never
   auto-stopped (cost-saver fully defeated), and
2. returned an empty `next_stop_at` → the SPA gated its countdown and Extend
   controls on a non-empty deadline, so both silently disappeared with no
   explanation.

## User-facing change

- **Cluster auto-stops again.** A jobstate row in an active status whose most
  recent timestamp is older than `AKS_AUTOSTOP_ACTIVE_ROW_STALE_SECONDS`
  (default 2 h) is treated as a crashed/`worker_lost` zombie and dropped from
  the active count. The idle clock then anchors on the real last activity, so an
  idle cluster stops as configured. A *genuinely* running job (recent timestamp)
  still keeps the cluster alive, and in-flight BLAST is independently protected
  by the live K8s `app=blast` probe, so a stale state row can never strand a
  running search.
- **The auto-stop panel always explains itself.** When auto-stop is enabled and
  the cluster is running but the evaluator returns a `keep` with no projected
  deadline (active job, cooldown, Extend grant, or a degraded read), the panel
  now renders a muted "Auto-stop armed · &lt;reason&gt;" line **and keeps the
  Extend button available** instead of showing nothing.

## API / IaC diff summary

- No HTTP contract change. `/api/aks/autostop/status` still returns the same
  shape; only the computed `verdict` / `active_job_count` / `next_stop_at`
  change for the zombie-row case.
- New optional env var `AKS_AUTOSTOP_ACTIVE_ROW_STALE_SECONDS` (default `7200` =
  2 h). No infra change required; the default is safe (Celery's hard task time
  limit is 3600 s, so a row untouched for 2 h — 2x the hard limit — is provably
  from a dead task). If an operator raises `CELERY_TASK_TIME_LIMIT` for
  genuinely long tasks, raise this above that limit too. `warmup` / `prepare_db`
  rows are NOT covered by the live `app=blast` probe, so this cap is their only
  over-stay guard; their node-warm K8s Jobs are short (~10-15 min) so 2 h never
  ages out a live warmup.

## Code changes

- `api/services/auto_stop_evaluator.py` — add `_ACTIVE_ROW_STALE_SECONDS`
  (default 2 h); `_scan_cluster_jobs` now ages out stale active rows (rows with
  no parseable timestamp fail safe and stay counted); `evaluate_cluster` passes
  `now`.
- `web/src/components/ClusterItem/AutoStopPanel.tsx` — add `armedNoDeadline`
  branch with explanation + Extend; add friendlier labels for
  `evaluator_unavailable` / `history_scan_truncated` / `power_state_unknown`.

## Validation

- `uv run pytest -q api/tests` → 3435 passed, 3 skipped.
  New tests: `test_stale_active_row_does_not_block_stop`,
  `test_fresh_active_row_still_blocks_stop`,
  `test_active_row_without_timestamp_fails_safe`,
  `test_stale_threshold_default_is_two_hours`
  (`api/tests/test_auto_stop_evaluator.py`).
- `uv run ruff check` on the changed backend files → clean.
- `cd web && npm run build` → success; `npm test -- --run src/components/ClusterItem` → 28 passed.
- Live evidence: `/api/aks/autostop/status` returned constant
  `verdict=keep, reason=active_jobs:1, next_stop_at=""` while all K8s
  `app=blast` jobs were `Complete` and the only stuck row was a stale
  `warmup` jobstate row frozen at `00:12` (Celery 1 h hard limit killed the
  task; the row never reached a terminal status).
