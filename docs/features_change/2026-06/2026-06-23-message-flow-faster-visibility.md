---
title: Message Flow — faster queue-arrival visibility (dedicated TTL + faster poll)
description: Give the Message Flow card a dedicated 10s snapshot TTL (env MONITOR_MESSAGE_FLOW_TTL_SECONDS) instead of the 30s monitor default, and poll the card every 5s idle / 4s active, so a request-queue message surfaces in ~5-10s instead of up to ~30-40s. True real-time push (SSE) tracked separately.
tags:
  - blast
  - ui
---

# Message Flow — faster queue-arrival visibility

## Motivation

A request that lands on the Service Bus request queue did not appear on the
dashboard Message Flow card immediately. The latency had three sources:

1. The card polled every 8s (active) / 10s (idle).
2. The backend snapshot was served from the shared monitor cache with a ~30s
   TTL — and an *externally* enqueued message does not invalidate that cache
   (only a dashboard submit, or the drain materialising a jobstate row, does).
3. A message that is queued but **not yet drained** (cluster warming up under
   `SERVICEBUS_QUEUE_AUTOSTART`, no consumer running, or one injected via the
   Azure portal) only shows through the live `queue_messages` preview, which
   rides the same 30s cache — so it could linger up to ~30s before surfacing.

Net effect: a queue arrival took ~10-20s in the common (drained) case and up to
~30-40s for a stuck/undrained message.

## User-facing change

* The Message Flow card now polls every **5s when idle** and **4s when active**
  (was 10s / 8s).
* The Message Flow snapshot now runs a **dedicated 10s TTL** instead of the 30s
  monitor default. This is applied per-call, so **only this card** refreshes
  faster — every other monitor card keeps `MONITOR_SNAPSHOT_TTL_SECONDS`. The
  snapshot cache is stale-while-revalidate, so the lower TTL refreshes in the
  background and never blocks a poll.
* New env knob `MONITOR_MESSAGE_FLOW_TTL_SECONDS` (default `10`) lets operators
  trade Service Bus peek frequency against freshness without a code change.

Net effect: a queue arrival now surfaces in ~5-10s, and a stuck/undrained queue
message within ~10s, without a manual refresh. The modal's manual **Refresh**
control (`refresh=true`, bypasses the cache) is unchanged for an on-demand
authoritative read.

## API / IaC diff summary

* `api/routes/monitor/message_flow.py`: add `_message_flow_ttl_seconds()` (env
  `MONITOR_MESSAGE_FLOW_TTL_SECONDS`, default 10s, fail-safe) and pass it as
  `ttl_seconds=` to the message-flow `cached_snapshot` call.
* `web/src/components/cards/MessageFlow/MessageFlowCard.tsx`: poll cadence
  10s/8s → 5s/4s.
* No IaC change. No new dependency.

## Not in this change (tracked separately)

True real-time push — eliminating the poll-wait entirely by pushing a refresh
event to the browser the instant the drain materialises a job — is a larger
build (SSE channel + ticket auth, reusing the existing
`publish_jobs_cache_invalidate` pub/sub). Tracked as a follow-up issue.

## Validation evidence

* `uv run ruff check api/routes/monitor/message_flow.py` — clean.
* `uv run pytest -q api/tests/test_message_flow.py` — 29 passed, including the
  new `test_message_flow_ttl_default_and_env_override` and
  `test_route_passes_dedicated_ttl` (default 10s + env override + fail-safe; the
  route passes the dedicated TTL and honours `refresh=true`).
* `cd web && npm run build` — type-checks and builds clean.
