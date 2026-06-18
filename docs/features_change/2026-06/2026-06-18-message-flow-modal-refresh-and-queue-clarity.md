---
title: Message Flow modal — manual refresh + queued-messages clarity
description: Add a cache-bypassing refresh control to the Message Flow modal and left-align/explain the queued-messages panel.
tags:
  - ui
  - blast
---

# Message Flow modal — manual refresh + queued-messages clarity

## Motivation

Two papercuts on the Service Bus **Message Flow** modal:

1. The bottom **Queued messages** panel inherited `text-align: center` from
   `.glass-dialog`, so the JSON payload and its heading rendered centred and
   were hard to read. The panel also gave no explanation of what the messages
   are, so an operator could not tell why the list was usually empty or what a
   lingering message meant.
2. The modal had no manual refresh control. Because the snapshot is served
   through the shared monitor cache (~30s TTL) and the card only polls every
   8–10s, a freshly-enqueued request-queue message could take up to ~30s to
   surface — and an operator had no way to force an authoritative reading.

## User-facing change

- The **Queued messages** panel is now left-aligned and carries a short
  explanation: these are the raw Service Bus request-queue messages (each one a
  BLAST search waiting for a cluster worker), read non-destructively (peeked),
  normally drained in under a second, and a lingering message means no worker
  has consumed it yet. The block below each entry is the message payload.
- The modal header gains a **Refresh** button (next to Close). It bypasses the
  ~30s snapshot cache, re-queries the Table + Service Bus synchronously, and
  writes the authoritative reading into the shared query cache so the card and
  the open modal update together. The icon spins and the button is disabled
  while a refresh is in flight.

> Note: a request-queue message that already drained (the normal sub-second
> case) cannot be shown by any refresh — it is gone before the peek runs. The
> refresh only removes the cache + poll latency for messages that linger.

## API / IaC diff summary

- `GET /api/monitor/message-flow` gains an optional `refresh: bool = false`
  query parameter. When `true`, the route passes `force=True` to
  `cached_snapshot`, bypassing the fresh/stale cache read and re-querying
  synchronously (the result is still stored for subsequent normal reads). The
  default (`false`) preserves the existing cached behaviour, so the per-poll
  card load is unchanged.
- No IaC change.

## Validation evidence

- `uv run pytest -q api/tests/test_message_flow.py api/tests/test_route_contracts.py`
  → 26 passed.
- `uv run ruff check api/routes/monitor/message_flow.py` → clean.
- `cd web && npm run build` + `npx tsc --noEmit` → clean.
- `npx eslint` on the three changed frontend files → clean.
- e2e `message-flow-events.ui.spec.ts` selectors unaffected (no button-count
  assertions; the new control is keyed by its own `aria-label`).
