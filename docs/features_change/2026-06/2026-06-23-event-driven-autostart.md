---
title: Event-driven queue-arrival auto-start (no 5-minute wait)
description: Trigger an immediate idle/auto-start evaluation the moment a request lands on the Service Bus queue, so a Stopped AKS cluster starts within seconds instead of waiting out the next 5-minute beat tick. Gated by SERVICEBUS_QUEUE_AUTOSTART, best-effort, de-duped by the single-flight lease.
tags:
  - blast
  - architecture
---

# Event-driven queue-arrival auto-start

## Motivation

Queue-arrival auto-start (commit `4cdd9f4`) starts a Stopped cluster when the
request queue holds undrained work — but the decision was evaluated only by the
`evaluate_idle_clusters` beat task, which runs every
`CELERY_BEAT_AKS_IDLE_AUTOSTOP_SECONDS` (default 300s). So a request enqueued
just after a tick waited up to ~5 minutes before the cluster even began starting.
Live verification confirmed the start worked but lagged a full beat interval.

The same enqueue already updates the Message Flow card instantly (submit-time
cache invalidation + the jobs-events SSE). This change hooks the auto-start
evaluation to that same enqueue event so the cluster starts within seconds.

## User-facing change

* The moment a request is enqueued onto the Service Bus request queue (the
  Service Bus Playground send, or a dashboard submit routed through the queue),
  an immediate `evaluate_idle_clusters` run is triggered. A Stopped cluster with
  an enabled auto-stop preference now begins starting within seconds instead of
  up to 5 minutes.
* No behaviour change when `SERVICEBUS_QUEUE_AUTOSTART` is off (default) — the
  trigger is gated, so the legacy poll-only path is unchanged.
* The 5-minute scheduled evaluation remains as the fallback (e.g. a request that
  arrives while the broker is briefly unreachable still gets picked up on the
  next tick).

## API / IaC diff summary

* `api/services/aks/queue_autostart.py`: new `request_autostart_evaluation()` —
  gated by `queue_autostart_enabled()`, best-effort enqueues
  `evaluate_idle_clusters.delay()`, never raises.
* `api/services/service_bus.py`: `send_request()` calls it after a successful
  enqueue (the single low-level producer, so both the Playground send and the
  submit-ingress enqueue are covered).
* No IaC change. No new dependency.

## Safety

* The single-flight cooldown lease in `evaluate_idle_clusters`
  (`acquire_autostart_lease`) de-dupes a burst of enqueues into at most one start
  per cooldown, so a flurry of messages cannot start-storm the cluster.
* `request_autostart_evaluation` is best-effort: a broker hiccup is swallowed and
  the scheduled beat tick remains the fallback, so it never fails a send.

## Validation evidence

* `uv run ruff check api/services/aks/queue_autostart.py api/services/service_bus.py` — clean.
* `uv run pytest -q api/tests/test_queue_autostart.py` — 19 passed, including the
  new gate-off no-op, gate-on enqueue, and swallow-broker-error tests.
* Live (customer dev): the poll-based path was confirmed working first — a
  Playground send drove `read_request_queue_depth` to 2 and the next beat tick
  logged `queue_autostart queued start cluster=elb-cluster-01 pending=2` →
  `queued_starts: 1`, and the cluster transitioned Stopped → Running. This change
  removes the up-to-5-minute lag before that start begins.
