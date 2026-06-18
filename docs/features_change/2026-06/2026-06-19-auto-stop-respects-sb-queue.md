---
title: Auto-stop respects Service Bus request-queue depth
description: AKS idle auto-stop keeps a Running cluster alive while the Service Bus request queue still holds undrained work, closing a decide-vs-act race that could strand the backlog.
tags:
  - operate
  - blast
---

# Auto-stop respects Service Bus request-queue depth

## Motivation

During a live Service Bus load/scale-tuning session we found an auto-stop
correctness bug: the AKS idle auto-stop evaluator only counted on-cluster
`app=blast` Jobs plus `jobstate` rows. When work was still queued in the
Service Bus request queue but had not yet been bridged to an `app=blast` Job
(the drain runs on a 10 s beat tick with a synchronous per-message OpenAPI
submit), the evaluator read `active=0` in the gap between drained jobs and
idle-stopped the cluster. The next drain attempt then hit a `ConnectTimeout`
against the now-stopped cluster and the backlog stranded (dead-letter risk).

## User-facing change

A Running AKS cluster is **no longer idle-stopped while the Service Bus
request queue still has pending (active/deliverable) messages**. The auto-stop
status reason surfaces as `sb_queue_pending:{N}`. This is additive protection
only — it never causes a stop, and an unreadable/disabled queue degrades
silently to the previous job-count decision so a cluster can never be stranded
running forever.

Behaviour is gated by `AKS_AUTOSTOP_RESPECT_SB_QUEUE` (default **on**); set it
to `false` / `0` / `no` to disable without a redeploy.

Out of scope (intentionally): auto-**start** of an already-Stopped cluster on
queue arrival, and surfacing `pending_queue_depth` in the autostop *status*
route (`/api/aks/.../autostop`) so the SPA countdown matches the beat decision
— that would add a Service Bus admin call per status poll.

## API / IaC diff summary

No HTTP contract or IaC change. Internal additions:

- `api/services/service_bus.py` — new `pending_request_count(cfg) -> int | None`
  (best-effort `active_message_count`; `None` on disabled/auth-fail/error;
  excludes scheduled and dead-lettered messages).
- `api/services/auto_stop_evaluator.py` — `evaluate_cluster(..., pending_queue_depth=None)`;
  a non-zero depth with no active jobs returns `keep` with reason
  `sb_queue_pending:{N}`. The new parameter is optional/keyword and defaults to
  `None`, so the existing `api/routes/aks/autostop.py` caller is unchanged.
- `api/tasks/azure/idle_autostop.py` — new `_sb_pending_signal(power_state)`
  (gated by `AKS_AUTOSTOP_RESPECT_SB_QUEUE`, Running-only, never raises); wired
  into both the decide and act `evaluate_cluster` calls; added to `__all__`.

## Validation evidence

- `uv run ruff check api` — clean.
- Wide regression sweep (155 passed):
  `uv run pytest -q api/tests/test_auto_stop_evaluator.py api/tests/test_auto_stop_live.py api/tests/test_servicebus_tasks.py api/tests/test_service_bus_entity_counts.py api/tests/test_idle_autostop_sb_queue.py api/tests/test_tasks_facade_contract.py api/tests/test_resident_consumer.py`
- New/updated tests: `test_idle_autostop_sb_queue.py` (5: power-state/env/disabled
  gates + delegation + degrade), `test_service_bus_entity_counts.py` (+3:
  `pending_request_count` active/failure/unconfigured), `test_auto_stop_evaluator.py`
  (+3: queue keeps idle cluster; 0/None allows stop; active-jobs precedence),
  `test_tasks_facade_contract.py` (registered `_sb_pending_signal`).
