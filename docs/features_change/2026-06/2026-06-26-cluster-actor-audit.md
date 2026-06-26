---
title: Actor audit on cluster lifecycle + auto-stop config
description: Stamp every AKS start/stop/scale/delete request and every auto-stop enable/disable PUT with the caller object_id as an App Insights cluster_lifecycle / autostop_config customEvent, and tag the system auto-stop path with actor=system:auto-stop so manual vs automatic stops are unambiguous.
tags:
  - operate
  - auth
---

# Actor audit on cluster lifecycle + auto-stop config

## Motivation

The Azure Activity Log shows that AKS stop/start was performed by the dashboard's
managed identity but not WHO inside the dashboard triggered it. To answer "who
stopped this cluster" / "who turned auto-stop off" without grepping unstructured
console logs, we now emit a structured App Insights customEvent at each action
site, with the actor explicitly recorded.

## User-facing change

* Every `POST /api/aks/{start,stop,scale,delete}` emits a `cluster_lifecycle`
  customEvent: `{action, actor="user", actor_oid, cluster, resource_group, task_id, …}`.
* `PUT /api/aks/autostop` emits an `autostop_config` customEvent with the new
  state AND the previous state (`enabled` / `idle_minutes`), so you can answer
  "who flipped it off" or "who shortened the idle window" directly.
* The system auto-stop path (`auto_stop_aks` Celery task) emits a
  `cluster_lifecycle` event with `actor="system:auto-stop"` and the evaluator
  reason (e.g. `idle:120m`) — it carries no `actor_oid`, so manual vs automatic
  stops cannot be confused.

## Design

* Reuses the existing `record_feature_event` helper (App Insights customEvents
  via `microsoft.custom_event.name`). No new sink, no new env var, no new
  dependency. Telemetry-off deployments still see the records in stdout logs.
* The route layer is the only place that knows the authenticated caller, so the
  event is emitted there (not inside the Celery task) — the task name carries
  the action and the event carries the actor.
* The system path emits its own event after the `stop_aks.run` call succeeds, so
  a system stop is recorded exactly once and a failed stop does not falsely
  attribute it.
* `actor` is a closed vocabulary today: `"user"` or `"system:auto-stop"`. The
  literal `system:` prefix can never collide with a caller object_id (GUID).

### KQL — who stopped it / who configured auto-stop

```kusto
customEvents
| where name == "cluster_lifecycle"
| where customDimensions.action == "stop"
| project timestamp, actor=tostring(customDimensions.actor),
          actor_oid=tostring(customDimensions.actor_oid),
          cluster=tostring(customDimensions.cluster),
          reason=tostring(customDimensions.reason)

customEvents
| where name == "autostop_config"
| project timestamp, actor_oid=tostring(customDimensions.actor_oid),
          cluster=tostring(customDimensions.cluster),
          enabled=tobool(customDimensions.enabled),
          prev_enabled=tobool(customDimensions.prev_enabled),
          idle_minutes=toint(customDimensions.idle_minutes),
          prev_idle_minutes=toint(customDimensions.prev_idle_minutes)
```

## API / IaC diff summary

* `api/routes/aks/lifecycle.py`: emit `cluster_lifecycle` on start/stop/scale/delete.
* `api/routes/aks/autostop.py`: emit `autostop_config` on PUT, with prev/new diff.
* `api/tasks/azure/idle_autostop.py`: emit `cluster_lifecycle` with
  `actor=system:auto-stop` after the system stop succeeds.
* No frontend / IaC change. Telemetry sink is the same `api.events` logger that
  the rest of the codebase uses.

## Validation evidence

* `uv run pytest -q api/tests/test_actor_audit.py` — 5 passed (start/stop/scale
  user actor, autostop PUT diff, system actor without actor_oid).
* `uv run ruff check api` — all checks passed.
* `uv run pytest -q api/tests` — 4677 passed, 3 skipped, 0 failed.
