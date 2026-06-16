---
title: Service Bus message peek in Playground and Message Flow
description: Non-destructive peek of request-queue messages so their count and sanitised content appear in the Service Bus Playground and the Message Flow card, matching what the Azure portal shows.
tags:
  - ui
  - user-guide
---

# Service Bus message peek in Playground and Message Flow

## Motivation

Messages enqueued from the Service Bus Playground were visible in the Azure
portal queue but never surfaced in the dashboard. The Playground could send and
drain messages but had no way to view the content of messages that were sitting
in the queue, and the Message Flow card only renders active `jobstate` Table
rows — never the raw queue messages. When messages lingered (consumer not
running, Manage claim missing for counts), the operator saw "no active messages"
in the dashboard while the portal clearly showed queued messages.

A non-destructive **peek** closes this gap. Peek uses only the data-plane
`Azure Service Bus Data Receiver` claim, so it works even when the admin
(`Manage`) claim needed for entity counts is unavailable and counts degrade to
`no_manage_claim`.

## User-facing change

- **Service Bus Playground**: a new "Peek messages" button in the Consumer pane
  lists the messages currently in the request queue without removing them —
  showing program, db, correlation/request id, and a sanitised, size-bounded
  body preview. Peek auto-refreshes after a send or drain when results are
  already open.
- **Message Flow**: the expanded modal now shows a "Queued messages" section
  listing the same peeked count + sanitised content. The compact card shows
  "N queued messages" instead of "no active messages" when the queue is
  non-empty but no jobs are in flight.

## API / IaC diff summary

- `api/services/service_bus.py`: added `_preview_message` and
  `peek_request_previews(cfg, max_count)` — sanitised, size-bounded
  (`_PEEK_BODY_MAX_CHARS = 4000`) JSON-safe previews built on the existing
  data-plane `peek_requests` (no settlement, no Manage claim).
- `api/routes/settings/service_bus.py`: added `GET /api/settings/service-bus/peek`
  (Reader-accessible, degrades gracefully, returns
  `{available, reason?, detail?, queue, messages, count}`).
- `api/tests/persona_reader_allowlist.py`: whitelisted the new `peek` route for
  the Reader persona.
- `api/services/message_flow.py`: `build_message_flow` now includes a bounded
  (`_QUEUE_PREVIEW_LIMIT = 10`) `queue_messages` preview, peeked only when
  counts are available or the only gap is the Manage claim (never a second slow
  connect when the namespace is unreachable).
- `web/src/api/settings.ts`, `web/src/api/messageFlow.ts`: new
  `ServiceBusPeekMessage` / `ServiceBusPeekResponse` types, `peekServiceBus`
  client, and optional `queue_messages` on `MessageFlowSnapshot`.
- `web/src/pages/ServiceBusPlayground.tsx`,
  `web/src/components/cards/MessageFlow/MessageFlowModal.tsx`,
  `web/src/components/cards/MessageFlow/MessageFlowCard.tsx`: UI wiring.

No IaC change. No new SAS token is issued to the browser; all peek traffic flows
through the `api` sidecar under the shared managed identity.

## Validation evidence

- `uv run ruff check api` → All checks passed.
- `uv run pytest -q api/tests` → 3829 passed, 3 skipped.
  - New `api/tests/test_service_bus_peek.py` (preview shaping, truncation, route
    degrade/available/auth-failure).
  - Extended `api/tests/test_message_flow.py` (queue_messages included; peek
    skipped when unreachable; peek runs when only the Manage claim is missing).
- `cd web && npm run build` → built successfully.
- `cd web && npm run lint` → clean.
