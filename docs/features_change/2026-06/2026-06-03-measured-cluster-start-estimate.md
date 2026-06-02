---
title: Measured cluster start/stop estimate
description: >-
  The cluster start guidance panel now derives its estimate from real measured
  AKS start/stop and OpenAPI deploy durations (median of recent samples),
  falling back to the previous built-in constants until samples accumulate.
tags:
  - user-guide
  - blast
---

# Measured cluster start/stop estimate

## Motivation

The cluster start guidance panel (`StartEstimatePanel`) previously showed a
hard-coded estimate ("Last observed AKS start took 4 min. OpenAPI usually adds
about 31 sec…"). The numbers were compile-time constants, not derived from what
the deployment actually experienced, so they could be misleading for a given
subscription / cluster size.

## User-facing change

The panel now displays an estimate computed from **real, measured** lifecycle
durations:

- Each time a cluster starts, stops, or its OpenAPI service deploys, the Celery
  task records the wall-clock duration for that phase (`aks_start`, `aks_stop`,
  `openapi_deploy`).
- The panel fetches `GET /api/monitor/aks/start-stats`, which returns the
  **median** of the most recent samples per phase, and shows
  "Median of the last N observed AKS starts is X".
- Until samples accumulate, the endpoint falls back to the previous built-in
  constants (235 s AKS start, 31 s OpenAPI deploy) tagged `source: "default"`,
  and the panel keeps the original "Typical AKS start is about X" wording.

## API / IaC diff summary

- New service `api/services/cluster_timings.py`: `record_timing(phase, seconds, …)`
  and `get_timing_stats(...) -> dict[str, PhaseStat]`. Persists to Azure Table
  `clustertimings` in deployed environments (inverse-timestamp RowKey for
  newest-first reads) or a local JSON file (`.logs/local/state/cluster_timings.json`)
  in dev. Median over the last 20 samples; best-effort writes that never fail the
  caller; reads degrade to defaults rather than raising.
- `api/tasks/azure/lifecycle.py`: `start_aks` / `stop_aks` now time the
  `begin_*` + `poller.result()` block and record `aks_start` / `aks_stop`.
- `api/tasks/openapi/deploy.py`: success path records `openapi_deploy`.
- `api/routes/monitor/aks.py`: new read-only route `GET /monitor/aks/start-stats`
  returning `{ phases: {…}, api_ready_seconds }`, degrading to an empty payload
  via `_graceful` (never 500).
- `web/src/api/monitoring.ts`: `ClusterTimingPhase` / `ClusterStartStats` types +
  `monitoringApi.aksStartStats()`.
- `web/src/components/ClusterItem/StartEstimatePanel.tsx`: TanStack Query fetch of
  the stats, with the constants retained as fallback.
- No IaC change — the new Azure Table is created on first write by the existing
  Storage account / shared MI.

## Validation evidence

- `uv run pytest -q api/tests/test_cluster_timings.py` → 8 passed (median,
  default fallback, unknown-phase rejected, out-of-range dropped, sample-limit,
  `to_dict` shape, route defaults `api_ready_seconds ≈ 266`, route reflects
  measurements).
- `uv run pytest -q api/tests` → 2431 passed, 3 skipped.
- `uv run ruff check api` → All checks passed.
- `cd web && npm run build` → tsc + vite build succeeded.
