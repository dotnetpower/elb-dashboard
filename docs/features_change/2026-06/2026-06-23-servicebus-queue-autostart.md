---
title: Service Bus queue-arrival AKS auto-start (default-OFF)
description: Add a default-OFF SERVICEBUS_QUEUE_AUTOSTART gate that starts a Stopped AKS cluster when the request queue holds undrained work, with a fail-closed cooldown lease, a drain readiness guard that defers draining during warmup, and lease rollback on enqueue failure.
tags:
  - blast
---

# Service Bus queue-arrival AKS auto-start

## Motivation

Idle auto-stop deallocates an AKS cluster after a grace window to save cost. Its
Service Bus keep-alive only *prevents* a stop while work waits — there was no
*start* side. So a BLAST request that arrives on the queue while the cluster is
already Stopped sat undrained until an operator manually started the cluster. The
deliberate inverse — start a Stopped cluster when the deployment-wide request
queue holds undrained work — was previously out of scope because a start spins up
billable compute. This change ships it behind a default-OFF gate, with the cost
and double-trigger risks designed out.

## User-facing change

* New gate `SERVICEBUS_QUEUE_AUTOSTART` (default-OFF). When unset, behaviour is
  unchanged — no queue probe, no auto-start, the legacy every-tick drain.
* When ON: the beat idle-autostop tick that *keeps* a Stopped cluster now also
  evaluates the request-queue depth once per tick (lazily, only when a Stopped
  cluster is actually seen). A positive pending depth enqueues an idempotent
  `start_aks`, rate-limited by a per-cluster cooldown lease.
* Cooldown / single-flight TTL is `SERVICEBUS_QUEUE_AUTOSTART_COOLDOWN_SECONDS`
  (default 600s, floored at 60s).

## Hardening (this change)

* **Drain readiness guard (R1):** while an auto-started cluster warms up, its
  OpenAPI plane is unreachable. Draining then would abandon-loop every received
  message (delivery-count burn → premature dead-letter) before the cluster is
  ready. When the gate is ON, `drain_and_resubmit` now defers the whole tick
  (`{"skipped": "cluster_not_ready"}`) until `external_blast.ready` succeeds, so
  the backlog — and thus the pending-depth start trigger — is preserved. No-op
  when the gate is OFF.
* **Lease rollback (R2):** the cooldown lease is reserved the moment
  `acquire_autostart_lease` returns True, on the assumption the caller enqueues
  `start_aks`. If that enqueue raises, `release_autostart_lease` rolls the
  reservation back so the next tick can retry immediately. Best-effort: a Redis
  error leaves the lease to expire via TTL (the safe direction — a start is at
  worst delayed, never duplicated).
* **Fail-closed lease (base):** a Redis error on acquire returns False (no start)
  — a missed start is cheap (the next tick retries) but a spurious start costs
  money.
* **Strict decision:** `should_autostart` returns True only for an exactly
  `Stopped` cluster (never `Stopping` / `Starting` / `Running` / blank-unknown)
  with a positive pending depth and the gate on, so a transient power-state blank
  or an in-flight start can never double-trigger.

## API / IaC diff summary

* `api/services/aks/queue_autostart.py` (new): `queue_autostart_enabled`,
  `should_autostart`, `acquire_autostart_lease`, `release_autostart_lease`.
* `api/services/auto_stop_sb_signal.py`: `read_request_queue_depth` (power-state
  agnostic queue read for the start side).
* `api/tasks/azure/idle_autostop.py`: `evaluate_idle_clusters` enqueues
  `start_aks` for a Stopped cluster with queued work; `queued_starts` summary
  counter; lease rollback on enqueue failure.
* `api/tasks/servicebus/tasks.py`: `_openapi_ready_for_drain` helper + gate-scoped
  readiness guard at the top of `drain_and_resubmit`.
* No IaC change. The gate is a plain env var defaulting OFF.

## Validation evidence

* `uv run ruff check api` — clean.
* `uv run pytest -q api/tests/test_queue_autostart.py api/tests/test_servicebus_tasks.py api/tests/test_auto_stop_task.py api/tests/test_auto_stop_sb_signal.py` — 90 passed.
* New tests: drain defers when gate ON + cluster not ready (queue not pulled),
  proceeds when ready, no readiness probe when gate OFF; `_openapi_ready_for_drain`
  True on success / False on probe error; lease release deletes the key /
  swallows a Redis error; idle-autostop rolls the lease back on enqueue failure.
* Real cluster-start behaviour is cost-bearing and verified separately after
  maintainer confirmation; the gate ships default-OFF so deploy is
  behaviour-neutral.
