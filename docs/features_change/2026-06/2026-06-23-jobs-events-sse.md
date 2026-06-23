---
title: Real-time job updates via SSE — instant Message Flow / Blast Jobs / AKS Jobs
description: Add an SSE channel (default-ON, kill-switch JOBS_EVENTS_SSE_DISABLED) that pushes a "jobs-changed" event to the browser the instant any job row changes, so the Message Flow card, the Blast Jobs list, and the AKS card Jobs refetch instantly instead of waiting out a poll. Works whether or not the Service Bus integration is enabled.
tags:
  - blast
  - ui
  - architecture
---

# Real-time job updates via SSE

## Motivation

Job-related dashboard surfaces (Message Flow, the Blast Jobs list, the AKS card
Jobs) all refresh by polling, so a change — a queue arrival, a direct submit, a
status transition — only appears after the next poll (seconds to a minute). The
backend already centralises "a job row changed" into a single invalidation
funnel (`jobs_cache_signal` / the submit route), and the repo already ships an
SSE + ticket pattern (the sidecar logs stream). This change bridges the two:
push the change to the browser so every job surface updates instantly, with
polling retained as the fallback.

## Service-Bus-agnostic by design

The event is "a job row changed", not "a queue drained". It fires from the
shared invalidation funnel that runs for **every** producer — a direct dashboard
submit with the Service Bus integration **disabled** triggers the same push as a
queue drain. The SSE endpoints never gate on `service_bus_enabled()`. The
Message Flow card still hides itself when Service Bus is off (unchanged), but the
Blast Jobs list and AKS Jobs — which are independent of Service Bus — get instant
updates regardless.

## User-facing change

* The feature ships **ON by default** with an env kill-switch
  `JOBS_EVENTS_SSE_DISABLED`. It is a purely additive UX improvement — polling
  remains the guaranteed fallback, so it can never revoke access — and the SSE
  transport is already proven by the logs/sidecars streams in the same topology,
  so it does not need the default-OFF soak that security *guards* require
  (charter §12a Rule 4 targets guards, not features). Set the kill-switch to shed
  the always-on connections; the ticket then returns `{"enabled": false}` and
  every card falls back to polling.
* The SPA opens **one** EventSource. On a `jobs-changed` event it invalidates the
  `["message-flow"]`, `["blast-jobs"]`, and `["aks-workload", "jobs"]` React Query
  caches, so all subscribed cards refetch at once. Polling stays on as a
  resilience fallback when the stream drops.
* **Not Service-Bus-conditional**: because the event is "a job row changed" (not
  "a queue drained"), the Blast Jobs list and AKS Jobs benefit even when the
  Service Bus integration is disabled, so the gate is unconditional rather than
  tied to `service_bus_enabled()`.

## API / IaC diff summary

* `api/services/jobs_events_bus.py` (new): thread-safe in-process fan-out bus
  (`register` / `unregister` / `broadcast_jobs_changed`). Per-client bounded
  `asyncio.Queue`; delivery marshalled onto the api event loop via
  `call_soon_threadsafe`; overflow coalesces (jobs-changed is idempotent).
* `api/routes/monitor/jobs_events.py` (new): `POST /api/monitor/jobs-events/ticket`
  (`require_caller`, single-use ticket, TTL ≤ 30s, IP/UA binding under
  `STRICT_SSE_TICKET_BINDING`) + `GET /api/monitor/jobs-events?ticket=…`
  (ticket-gated SSE; 204 on gate-off / bad ticket so EventSource stops
  auto-reconnecting). Reuses `sse_ticket.py` exactly like the logs stream.
* `api/services/blast/jobs_cache_signal.py` + `api/routes/blast/submit.py`: the
  two invalidation funnels now also call `broadcast_jobs_changed` (best-effort,
  never raises) — covering the queue-drain path and the Service-Bus-disabled
  direct-submit path respectively.
* `api/routes/monitor/__init__.py`: register the new router.
* `web/src/api/jobsEvents.ts` + `web/src/hooks/useJobsEvents.ts` (new) +
  `web/src/App.tsx`: the ticket client and the single app-root hook that opens
  the stream and invalidates the job caches.
* No IaC change. No new dependency.

## Security

* SSE stays ticket-gated — no `Depends(require_caller)` on the stream endpoint
  (EventSource cannot send bearer headers, charter §12a Rule 5). The ticket is
  single-use, short-TTL, origin-checked, and IP/UA-bound under the existing
  `STRICT_SSE_TICKET_BINDING` flag — identical contract to the logs SSE.
* Default-ON with an env kill-switch (`JOBS_EVENTS_SSE_DISABLED`): the feature is
  additive (polling fallback always present) so it cannot revoke access, and the
  endpoints are inert when the kill-switch is set (ticket → disabled, stream → 204).

## Validation evidence

* `uv run ruff check api/...` (touched files) — clean.
* `uv run pytest -q api/tests/test_jobs_events_bus.py api/tests/test_jobs_events_route.py`
  — 11 passed (bus delivery from a non-loop thread, overflow coalesce, default-ON
  ticket issuance, kill-switch → disabled + stream 204, ticket single-use, and
  BOTH funnels broadcast — including the Service-Bus-disabled direct-submit path).
* `uv run pytest -q api/tests/test_jobs_cache_signal.py` — 8 passed (funnel
  unchanged besides the additive broadcast).
* `cd web && npm run build` — type-checks and builds clean; `eslint` clean.

## Relationship to the Message Flow TTL change

The earlier per-card TTL + faster poll (commit `a37f841`) remains as the
poll-based fallback path; this SSE channel is the real-time path on top. With the
gate on, a queue arrival / submit surfaces in well under a second; with it off,
the faster poll still bounds it to ~5-10s.
