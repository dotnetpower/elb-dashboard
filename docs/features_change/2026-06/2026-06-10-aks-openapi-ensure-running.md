---
title: OpenAPI ensure-running readiness gate
description: A dashboard-driven endpoint that reports a stopped/starting/warming/ready phase for the AKS cluster hosting elb-openapi and wakes it on demand.
tags:
  - blast
  - operate
---

# OpenAPI ensure-running readiness gate

## Motivation

The [OpenAPI](https://www.openapis.org/) plane (`elb-openapi`) runs **inside** the
AKS cluster. When that cluster is stopped, the OpenAPI service is down with it, so
an external caller cannot ask OpenAPI to wake its own host. The always-on dashboard
`api` sidecar (Container Apps, `minReplicas: 1`) is the only component that stays up,
so it must be the one to check cluster state and start it.

Callers also need to distinguish *"stopped"* from *"starting"* from *"warmed and
ready to serve"* — submitting a BLAST request against a cluster whose node-local DB
cache is still cold wastes a run.

## User-facing change

New endpoint:

```
POST /api/aks/openapi/ensure-running
```

Body: `resource_group` and `cluster_name` are required; `subscription_id` defaults to
the `AZURE_SUBSCRIPTION_ID` env. Optional `start=false` observes without starting;
optional `auto_warmup` / `auto_openapi` mirror `POST /api/aks/start` and are only used
when a start is actually enqueued.

The response carries a single polled `status` that transitions across polls:

| `status` | meaning | `Retry-After` |
|----------|---------|---------------|
| `not_found` | cluster does not exist in ARM | — |
| `stopped` | cluster is stopped; a start is enqueued when allowed (`start_triggered=true`) | 30 s |
| `starting` | start LRO in progress | 30 s |
| `warming` | cluster Running but warmup nodes not yet Ready | 15 s |
| `ready` | Running and (warmup complete, or no warmup configured) — safe to serve | — |
| `unknown` | ARM unreachable; no start is triggered, retry later | 30 s |

A start is enqueued **only** for a fully-stopped cluster (never while `Stopping`/
`Starting`), so polling the endpoint cannot race an in-flight lifecycle LRO or rack up
cluster-start cost. A cluster whose warmup readiness cannot be confirmed degrades to
`warming` (never `ready`). Auto-start can be disabled with `ENSURE_RUNNING_AUTO_START=false`.

## API / IaC diff summary

- `api/services/monitoring/aks.py` — new `get_aks_cluster_snapshot()` (single
  `ManagedClusters.get` + the existing serialiser) re-exported from
  `api/services/monitoring/__init__.py`.
- `api/services/aks/ensure_running.py` — new pure state machine
  `evaluate_ensure_running()` (cached ARM health + live warmup readiness gate when
  Running and warmup is configured). No side effects.
- `api/routes/aks/ensure_running.py` — new route under `aks_router`; enforces
  `require_caller`, enqueues the existing `start_aks` task on the `stopped` +
  recommended path, sets `Retry-After`.
- `api/routes/aks/__init__.py` — wires + re-exports the new router/handler.
- No Bicep / IaC change.

## Validation evidence

- `uv run pytest -q api/tests/test_aks_ensure_running.py` → 14 passed (every phase +
  the route start/no-start/Retry-After/auto-start-disabled contracts).
- `uv run pytest -q api/tests` → 3244 passed, 3 skipped.
- `uv run ruff check` clean on all touched files.
