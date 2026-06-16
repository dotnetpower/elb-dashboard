---
title: Service Bus dead-letter queue inspect / delete / promote
description: Operators can now peek the Service Bus dead-letter queue, delete specific messages, or promote them back onto the request queue for a retry — all from the Service Bus Playground.
tags:
  - blast
  - operate
  - ui
---

# Service Bus dead-letter queue management

## Motivation

The Service Bus Playground could already peek the **request** queue and show its
runtime counts (including a dead-letter total), but a dead-lettered message was a
dead end: the operator could see "3 dead-letter" and run an all-or-nothing purge,
but could not look at *why* each message failed, delete a specific one, or
re-queue a message whose failure was transient (e.g. the `elb-openapi` pod was
down during a cluster stop/start, which is exactly how the 3 DLQ messages in the
live deployment got there — see the 2026-06-16 E2E report).

## User-facing change

The Service Bus Playground consumer pane gains a **dead-letter** panel:

* **Inspect dead-letter** — non-destructive peek of the DLQ. Each message shows
  its `sequence_number`, dead-letter reason + error description, delivery count,
  program/db, and a sanitised body preview. Reader-accessible (data-plane
  Receiver claim only, like the request-queue peek).
* **Promote (N)** — re-queues the selected messages onto the main request queue
  so the next drain bridges them to BLAST execution. The re-send happens *before*
  the DLQ removal, and the drain handler dedupes on `external_correlation_id`, so
  a message is never lost and never causes a duplicate run.
* **Delete (N)** — permanently deletes the selected messages. The SPA confirms
  via the explicit selection; the server caps the batch.

Selection is by `sequence_number` (the stable handle the peek exposes), with a
select-all / clear toggle.

## API / IaC diff summary

### Backend (`api/`)

* `api/services/service_bus.py`:
  * `peek_dead_letter` / `peek_dead_letter_previews` — DLQ counterpart of
    `peek_requests` / `peek_request_previews`; previews add
    `dead_letter_reason` / `dead_letter_error_description` / `delivery_count`.
  * `ParsedMessage` gains `dead_letter_error_description` + `delivery_count`
    (populated in `_parse`).
  * `DeadLetterActionStats` + `delete_dead_letter_messages` /
    `promote_dead_letter_messages` — targeted by `sequence_number`, bounded
    (by `max_messages` and by stopping once every requested seq is matched),
    partial-failure isolated. Promote re-sends to the main queue **before**
    completing the DLQ message (at-least-once safety; the idempotent drain
    handler collapses any duplicate).
* `api/routes/settings/service_bus.py`:
  * `GET /dlq/peek` (Reader-accessible, read-only), `POST /dlq/delete`,
    `POST /dlq/promote` (both write-gated, 409 when the integration is off,
    structured 400 on a missing / non-integer `sequence_numbers` list, batch
    capped at `_DLQ_ACTION_MAX = 200`).
* `api/tests/persona_reader_allowlist.py` — adds `dlq_peek` (read-only, mirrors
  the existing `peek` entry). `dlq_delete` / `dlq_promote` are intentionally NOT
  allowlisted (write actions — a Reader is blocked by RBAC).

### Frontend (`web/`)

* `web/src/api/settings.ts` — `ServiceBusDlqMessage` / `ServiceBusDlqPeekResponse`
  / `ServiceBusDlqActionResponse` types + `peekServiceBusDlq` /
  `deleteServiceBusDlq` / `promoteServiceBusDlq` client methods.
* `web/src/pages/ServiceBusPlayground.tsx` — `DlqPanel` + `DlqMessageItem`
  components wired into the consumer pane under the dead-letter count, with
  delete/promote mutations that invalidate the status query and refresh the
  DLQ peek.

## Validation evidence

* `api/tests/test_service_bus_dlq.py` (new, 7): peek surfaces reason +
  sequence_number; delete targets only the requested sequence and stops once
  matched; promote re-sends then completes, keeps the message in the DLQ when
  the re-send fails, and no-ops on an empty list.
* `api/tests/test_service_bus_peek.py` (extended, +8): DLQ peek/delete/promote
  routes — available / not-configured / disabled (409) / missing-sequence (400)
  / non-integer (400) / de-dup before service / invokes service.
* `api/tests/test_persona_matrix.py` — green with the new `dlq_peek` allowlist
  entry; delete/promote stay write-gated.
* Full backend: `uv run pytest -q api/tests` → 3858 passed, 3 skipped.
* `uv run ruff check` clean; `cd web && npx tsc --noEmit` + `npx eslint` clean;
  `npm run build` succeeds.

## Design notes (self-critique)

* **At-least-once promote**: re-send precedes DLQ completion. A crash between the
  two leaves the message in both queues, but the drain handler's
  `get_bridge(correlation_id)` idempotency check (and the sibling's
  idempotency-key dedup) collapses it to one job — never a lost message, never a
  duplicate run.
* **Bounded**: every DLQ receive loop is capped by `max_messages` and exits early
  once all requested sequence numbers are matched, so "delete these 3" never
  scans the whole backlog.
* **Partial failure**: a per-message settle failure is counted (`failed`) and the
  loop continues; one bad message never aborts the batch.
* **Security**: delete/promote are write-gated (not Reader-allowlisted); DLQ peek
  output is run through `sanitise` and length-bounded; no SAS token reaches the
  browser.
