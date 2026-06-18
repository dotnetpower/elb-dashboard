---
title: Auto-stop status route reflects Service Bus queue depth
description: The autostop status route now feeds the cached Service Bus request-queue depth into the evaluator so the SPA countdown agrees with the auto-stop beat decision, with a shared TTL-cached signal that bounds the admin-call rate.
tags:
  - operate
  - blast
  - ui
---

# Auto-stop status route reflects Service Bus queue depth

## Motivation

The [previous change](2026-06-19-auto-stop-respects-sb-queue.md) made the
auto-stop **beat driver** keep a Running AKS cluster alive while the Service Bus
request queue still held undrained work, but left the autostop **status** route
(`/api/aks/.../autostop/status`) unaware of the queue. That route is what the
SPA polls to render the "Stops in mm:ss" countdown, so the dashboard could show
an idle countdown ticking down while the beat had already decided to keep the
cluster — a confusing decide-vs-display mismatch flagged as the out-of-scope
follow-up (issue #52, step 4).

The reason it was deferred was cost: a naive implementation issues one Service
Bus admin call (`get_queue_runtime_properties`) per status poll per cluster.

## User-facing change

The autostop status route now passes the Service Bus request-queue depth into
the evaluator, so the SPA countdown matches the beat decision: a Running cluster
with queued work shows the kept state (`reason=sb_queue_pending:N`) instead of a
shrinking idle countdown. The AutoStop panel renders this as
`"N requests queued in Service Bus — staying running."`.

The admin-call cost is bounded by a **deployment-global TTL cache** (default 5s):
the request queue is a single deployment-wide entity, so one admin read serves
every cluster card and every concurrent browser within the window, regardless of
how many clusters are polling. Same env gate as before
(`AKS_AUTOSTOP_RESPECT_SB_QUEUE`, default on); an unreadable/disabled queue
degrades to the prior job-count decision (additive only).

## API / IaC diff summary

No HTTP contract change (the `reason` field already documents free-form codes).
Internal changes:

- New `api/services/auto_stop_sb_signal.py` — `pending_queue_signal(power_state, *, ttl_seconds=5.0)`:
  the shared, gated, TTL-cached request-queue signal. `ttl_seconds <= 0` bypasses
  the cache. Never raises.
- `api/tasks/azure/idle_autostop.py` — `_sb_pending_signal` now delegates to the
  shared signal with `ttl_seconds=0` (the beat act-path keeps reading the live
  queue); the inline gating + `import os` were removed.
- `api/routes/aks/autostop.py` — `_status_pending_queue_depth(power_state)` feeds
  the cached signal into the status route's `evaluate_cluster` call.
- `web/src/components/ClusterItem/AutoStopPanel.tsx` — `reasonText` renders the
  `sb_queue_pending:N` code; `web/src/api/aks.ts` doc comment lists the new code.

## Validation evidence

- `uv run ruff check api` — clean.
- `uv run pytest -q api/tests/test_auto_stop_sb_signal.py api/tests/test_idle_autostop_sb_queue.py api/tests/test_tasks_facade_contract.py api/tests/test_auto_stop_evaluator.py api/tests/test_aks_autostop_route.py`
  — 128 passed. New `test_auto_stop_sb_signal.py` covers the gates, cache-hit
  (one admin call within TTL), cache-bypass (`ttl=0`), cache re-read, and degrade.
- `cd web && npm run build` — succeeds.
