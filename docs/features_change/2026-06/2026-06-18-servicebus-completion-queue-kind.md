---
title: Service Bus completion entity — queue/queue option
description: Make the optional Service Bus completion entity selectable as a topic (fan-out, default) or a queue (point-to-point), enabling a queue/queue topology.
tags:
  - blast
  - architecture
---

# Service Bus completion entity: topic (default) or queue

## Motivation

The optional Service Bus BLAST integration was queue/topic: requests on the
`elastic-blast-requests` **queue**, completion transition events on the
`elastic-blast-completions` **topic**. The topic gives fan-out — the
in-deployment demo observer and any number of external subscribers each receive
their own copy of every completion event. A deployment that only ever runs a
single completion consumer does not need fan-out and may prefer a simpler
**queue/queue** topology.

This change makes the completion entity kind configurable while preserving the
historical topic/fan-out behaviour by default (charter §12a Rule 4: unset =
existing behaviour).

## User-facing change

- New deployment-level override `SERVICEBUS_COMPLETION_KIND` = `topic` (default)
  or `queue`. Like the existing `SERVICEBUS_REQUEST_QUEUE` /
  `SERVICEBUS_RESPONSE_TOPIC` entity-name overrides, a well-formed value wins
  over the saved Settings config; an unrecognised value is ignored (logged) and
  the saved/default kind stands.
- A persisted `completion_kind` field is added to the Service Bus config row so
  the value round-trips through the API (`public_dict`). The env override is the
  recommended, footgun-free way to pin queue/queue (the Settings form does not
  yet render a kind selector; until it does, a PUT that omits the field would
  reset it to `topic`, but the env override always wins on read).
- In **queue** mode the in-deployment demo observer is intentionally **not**
  started — a queue is point-to-point, so the observer would steal messages from
  the real external consumer. The standalone consumer
  (`python -m api.services.service_bus_external_consumer` with
  `SERVICEBUS_COMPLETION_KIND=queue`) is the queue consumer.
- `entity_counts` now returns `completion_kind`; in queue mode it surfaces the
  completion queue's runtime counters as a single pseudo-subscription row so the
  Message Flow card keeps rendering unchanged.
- The standalone examples under `example/servicebus/` gained
  `--completion-kind topic|queue` (consume.py) and a `completion_kind` field in
  the monitor snapshot; the README documents the trade-off.

## Trade-off (explicit)

| Kind | Model | Consequence |
| --- | --- | --- |
| `topic` (default) | Fan-out: one copy per subscription | Multiple independent subscribers + the dashboard observer each get every event. |
| `queue` | Point-to-point: one competing consumer | Simpler queue/queue topology, but only one consumer receives each event; the demo observer is disabled so it cannot compete. |

## API / IaC diff summary

- `api/services/service_bus_pref.py`: `completion_kind` field + constants,
  `SERVICEBUS_COMPLETION_KIND` env overlay, `completion_is_queue()` helper.
- `api/services/service_bus.py`: `publish_event` and `entity_counts` branch on
  the completion kind (queue sender / queue runtime counts vs topic sender /
  subscription listing).
- `api/services/service_bus_external_consumer.py`: `consume_completions(kind=…)`
  selects a queue receiver vs subscription receiver; the worker demo path is
  disabled in queue mode; the standalone entry point honours the env kind.
- `example/servicebus/consume.py`, `monitor.py`, `README.md`: queue-mode wiring
  + docs.
- No Bicep/IaC change — the Service Bus entities are bring-your-own; infra only
  carries the `SERVICEBUS_ENABLED` gate.

## Validation evidence

- `uv run ruff check api example/servicebus` — clean.
- `uv run pytest -q api/tests` — 3928 passed, 3 skipped.
- New tests: completion-kind default/override/coercion
  (`test_service_bus_pref.py`, `test_service_bus_env_override.py`), queue-kind
  publish (`test_service_bus_drain_loop.py`), queue-kind counts
  (`test_service_bus_entity_counts.py`), queue receiver + queue-mode worker skip
  (`test_service_bus_external_consumer.py`).
- `python example/servicebus/{consume,monitor,send_request}.py --self-test` — all OK.
