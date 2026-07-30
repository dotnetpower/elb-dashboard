---
title: Service Bus queue and response health telemetry
description: Add periodic payload-free App Insights health snapshots for direct producer queues, drain admission, completion wiring, and durable response outbox liveness without changing customer message contracts.
tags:
  - blast
  - operate
---

# Service Bus queue and response health telemetry

## Motivation

External services can send directly to the Azure Service Bus request queue. That
path does not pass through the dashboard producer helper, so no application-level
`enqueued` event exists until the worker receives the message. Lifecycle
admission can intentionally leave messages broker-owned for an extended period,
and transient completion publication can leave responses in the durable outbox.
Operators therefore need a low-cardinality deployment-level signal that
separates pending, blocked, dead-lettered, and response-delivery states.

## User-facing change

When the Service Bus integration is enabled, the reconcile worker emits one
`servicebus_health` Application Insights custom event every five minutes. The
event contains only bounded operational scalars:

- request queue active, scheduled, total, and dead-letter counts;
- completion configuration, kind, subscription count, active count, and
  dead-letter count;
- durable response outbox sampled pending count, truncation flag, oldest age,
  and the current worker process's latest flush outcome;
- drain admission availability, allowed state, and bounded reason;
- resident consumer enablement and configured drain concurrency.

Warning codes identify missing/unreachable completion entities, completion
topics with no subscriptions, unavailable counters or outbox storage, non-empty
request/completion DLQs, truncated outbox backlog, flush failure, and active
requests blocked by execution admission. Warning logs are emitted only when the
warning set changes, avoiding a five-minute log flood.

## Compatibility and security

The customer request body and `blast.transition` response schema are unchanged.
No status, phase, required field, Service Bus entity, Azure role, or network rule
is added. Request-only deployments continue processing and receive an internal
`completion_not_configured` warning only.

The telemetry helper has an explicit scalar-only signature. Query FASTA, BLAST
options, raw messages, response payloads, credentials, and connection strings
cannot be passed to the event. Queue and outbox reads are independent and
bounded; one failed signal degrades without hiding the others.

## Validation evidence

- `uv run pytest -q api/tests/test_service_bus_health.py`
- `uv run pytest -q api/tests/test_service_bus_observability.py`
- `uv run pytest -q api/tests/test_celery_queue_isolation.py`
- `uv run pytest -q api/tests/test_servicebus_tasks.py api/tests/test_service_bus_outbox.py`
- `uv run ruff check api`
