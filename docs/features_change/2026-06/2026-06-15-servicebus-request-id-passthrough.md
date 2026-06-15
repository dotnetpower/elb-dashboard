---
title: Service Bus request_id pass-through to the completion topic
description: A caller-supplied request_id on a BLAST request queue message is now preserved end-to-end onto every completion-topic event and its envelope.
tags:
  - blast
  - architecture
---

# Service Bus `request_id` pass-through to the completion topic

## Motivation

An external producer that enqueues a BLAST request onto the Service Bus request
queue (`elastic-blast-requests`) often carries its own correlation/tracking value
— e.g. `request_id`. Until now only the server-derived `external_correlation_id`
survived to the completion topic (`elastic-blast-completions`); any other
caller-supplied value the producer set on the request message was dropped at the
bridge, so a topic subscriber could not correlate completions back to its own
request id.

## User-facing change

- If a request queue message carries a `request_id` (in the JSON body, or as a
  Service Bus application property of the same name), that value is now:
  - persisted on the bridge row,
  - echoed onto **every** completion-topic `blast.transition` event body
    (`queued` → `running` → terminal, plus the `bridge_timeout` failure event),
  - stamped on the published topic message **envelope**
    (`application_properties["request_id"]`) so a subscriber can correlate or
    filter without parsing the payload,
  - preserved in the dashboard's observed-completions store (visible in the
    Service Bus Playground).
- The Service Bus Playground send form gains an optional **`request_id`** input,
  and the observed-completions list shows the `request_id` badge when present —
  so the whole round-trip can be exercised browser-only.
- Pass-through is bounded to 256 chars and is **never** injected into the OpenAPI
  submit payload (it is a tracking value, not a BLAST submit option). It does not
  change the event dedup `event_id` (which stays `sha256(corr:status)`), so
  at-least-once dedup semantics are unchanged.

## API / IaC diff summary

- `api/services/service_bus_tracking.py`: `BridgeRecord` gains `request_id`
  (persisted via existing `to_dict`/`from_dict` round-trip).
- `api/tasks/servicebus/tasks.py`: `_extract_request_id()` (body → application
  property fallback, trimmed + length-bounded); `_transition_event()` adds an
  optional `request_id`; drain stores it on the bridge and stamps the queued
  event; the transition publisher echoes `rec.request_id` on every event.
- `api/services/service_bus.py`: `publish_event()` stamps
  `application_properties={"request_id": …}` on the topic message when present.
- `api/services/service_bus_completions.py`: observed-completion entry stores
  `request_id`.
- `api/routes/settings/service_bus.py`: the Playground send route pops
  `request_id` before OpenAPI validation, re-attaches it to the enqueued message
  body, and echoes it in the send/dry-run responses.
- `web/src/api/settings.ts` + `web/src/pages/ServiceBusPlayground.tsx`: optional
  `request_id` on send + observed-completion types, a form input, and a list
  badge. No IaC change.

## Validation evidence

- `uv run pytest -q api/tests` → 3710 passed, 3 skipped.
- New/updated tests:
  - `api/tests/test_servicebus_tasks.py::test_extract_request_id_body_then_props_then_missing`
  - `…::test_transition_event_includes_request_id_when_present`
  - `…::test_drain_propagates_request_id_to_bridge_and_queued_event`
  - `…::test_publish_transitions_echoes_request_id`
  - `api/tests/test_service_bus_drain_loop.py::test_publish_event_stamps_request_id_on_envelope`
  - `…::test_publish_event_no_request_id_leaves_envelope_clean`
  - `api/tests/test_service_bus_completions.py::test_record_preserves_request_id`
  - `api/tests/test_settings_service_bus.py::test_send_propagates_request_id_into_queue_body`
  - `…::test_send_dry_run_echoes_request_id`
- `uv run ruff check` on all touched paths → clean.
- `cd web && npm run build` → built (tsc clean).
</content>
</invoke>
