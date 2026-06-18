---
title: Service Bus jobs surface on the dashboard without waiting out the cache TTL
description: Cross-sidecar cache invalidation so a queue-ingested BLAST request appears on Recent searches, the Dashboard jobs card, and the Message Flow card on the next poll instead of up to 30 s late.
tags:
  - blast
  - architecture
---

# Service Bus requests surface on the dashboard quickly

## Motivation

A BLAST request that enters the Service Bus request queue showed up **too late**
on the dashboard — Recent searches, the main Dashboard jobs card, and the Message
Flow card all lagged the moment the request was actually queued/running. The
producer side was fine; the delay was entirely on the **read/display** side.

Two compounding root causes:

1. **api-process producer didn't drop its own caches.** The Service Bus
   Playground send route (`POST /api/settings/service-bus/send`) writes a
   send-time `queued` placeholder jobstate row so the job is visible the instant
   it lands on the queue — but it did **not** invalidate the api sidecar's
   in-process read caches (jobs-list SWR ~10 s, monitor `message-flow` snapshot
   ~30 s, external `/v1/jobs` discovery ~70 s). So the placeholder row existed
   immediately yet the cached listings kept serving the job-less snapshot for up
   to the full TTL. The BLAST submit route already busts these; the Playground
   send route simply never did.

2. **worker-materialised rows can't reach the api caches.** The request-queue
   drain runs in the **worker** sidecar and creates the durable jobstate row
   there. Those caches are **in-process to the api sidecar**, so the worker has
   no way to invalidate them — a queue-ingested job (especially one from an
   external producer, which writes no placeholder) waited out the cache TTL
   before surfacing.

## User-facing change

- A request sent through the Service Bus Playground now appears on Recent
  searches / the Dashboard jobs card / the Message Flow card on the **next poll**
  instead of up to ~30 s later.
- A request drained from the queue by the worker (including external producers
  that post directly to the queue) now busts the api caches cross-process, so the
  freshly created row surfaces on the next poll rather than waiting out the TTL.
- No behaviour change when the Service Bus integration is off, and no change to
  what the cards display — only how quickly a new row becomes visible.

## API / IaC diff summary

- New `api/services/blast/jobs_cache_signal.py` — best-effort cross-sidecar
  invalidation for the three job-visibility caches, mirroring the proven
  `db_metadata` Redis pub/sub pattern:
  - `invalidate_jobs_visibility_caches_local()` drops the jobs-list +
    `monitor:message-flow` + external-jobs caches (isolated, never raises).
  - `publish_jobs_cache_invalidate()` / `notify_jobs_cache_changed()` —
    cross-sidecar publish (worker) and local+publish (api producer).
  - `start_jobs_cache_subscriber()` / `stop_jobs_cache_subscriber()` — api-only
    subscriber thread; honours `JOBS_CACHE_INVALIDATE_DISABLED=true`.
- `api/app/lifespan.py` — start/stop the subscriber alongside the existing
  `db_metadata` one (api sidecar only).
- `api/routes/settings/service_bus.py` — the Playground send route calls
  `notify_jobs_cache_changed()` after writing the placeholder (in-process, so the
  local invalidate is immediate).
- `api/tasks/servicebus/tasks.py` — the worker drain handler publishes the
  invalidation after creating the durable row (and on the placeholder
  fail/reject paths) so the api sidecar drops its caches.
- `api/tests/conftest.py` — default `JOBS_CACHE_INVALIDATE_DISABLED=true` in the
  suite (no daemon thread / real Redis), matching the `db_metadata` guard.
- No IaC change. The optional `SERVICEBUS_RESIDENT_CONSUMER=true` knob (already
  shipped, default OFF) remains the complementary lever to cut the drain latency
  itself from the ~10 s beat interval to ~1 s.

## Validation evidence

- `uv run pytest -q api/tests/test_jobs_cache_signal.py` — 8 passed (local trio,
  failure isolation, disabled gate, publish-to-channel, notify, subscriber no-op
  + subscriber-invalidates-on-message via a fake Redis pub/sub).
- `uv run pytest -q api/tests/test_servicebus_tasks.py` — 35 passed (drain
  handler with the new publish calls).
- `uv run pytest -q api/tests/test_settings_service_bus.py
  api/tests/test_servicebus_placeholder.py api/tests/test_smoke.py` — 126 passed
  (with local Redis).
- `uv run pytest -q api/tests/test_message_flow.py
  api/tests/test_blast_jobs_routes.py api/tests/test_jobs_list_cache.py
  api/tests/test_monitor_cache.py` — 69 passed.
- `uv run ruff check` — clean on all touched files.
