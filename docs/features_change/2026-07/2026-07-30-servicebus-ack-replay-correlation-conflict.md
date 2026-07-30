---
title: Service Bus duplicate ACK replay and correlation conflict protection
description: Prevent ACK timeouts and ambiguous deduplication by fingerprinting execution payloads, replaying accepted events for exact retries, and rejecting correlation collisions.
tags:
  - blast
  - architecture
  - operate
---

# Service Bus duplicate ACK replay and correlation conflict protection

## Motivation

[Azure Service Bus](https://learn.microsoft.com/azure/service-bus-messaging/service-bus-messaging-overview)
request delivery is at-least-once. The drain previously treated every existing
`external_correlation_id` bridge as an unconditional duplicate: it completed the
new request message without replaying a queued acceptance event. A caller using
a new `request_id` could therefore wait until its timeout even though the
original BLAST job existed. The same behavior also silently collapsed two
logically different requests that reused one correlation id.

The resident consumer additionally ignored the configured bounded drain
concurrency, and the bundled completion observer joined the shared `default`
subscription by default.

## User-facing change

- Every new bridge stores only a SHA-256 fingerprint of its canonical execution
  payload. Tracking-only metadata is excluded, and no query body is persisted in
  the bridge or emitted in conflict logs/events.
- An exact retry reuses the existing OpenAPI job and republishes a queued
  acceptance event carrying that request message's `request_id`. The request is
  completed only after this replay publish succeeds; failure abandons it for
  safe redelivery.
- Reusing a correlation id for different execution semantics publishes a
  terminal `failed` event with
  `error_code=servicebus_correlation_conflict`, then dead-letters the conflicting
  message without starting another BLAST execution.
- The resident consumer uses the same bounded `SERVICEBUS_DRAIN_CONCURRENCY`
  resolver as the beat drain while retaining the atomic-claim safety check.
- The bundled demo observer defaults only to `playground-observer`. An explicit
  `default` override still emits the existing strong warning, and queue mode
  still disables the observer to avoid competing for external acknowledgements.

`external_correlation_id` must be unique for every logical request. Inclusive
and exclusive searches must not share it. `request_id` remains tracing metadata,
not an idempotency key.

## API / IaC diff summary

- `api/tasks/servicebus/tasks.py`: canonical execution fingerprinting, duplicate
  queued-ACK replay, conflict event and dead-letter policy.
- `api/services/service_bus_tracking.py`: backward-compatible bridge fingerprint
  persistence in the existing Table/file record.
- `api/services/blast/resident_consumer.py`: configured bounded drain
  concurrency propagation.
- `api/services/service_bus_external_consumer.py`: dedicated observer-only
  default subscription.
- No route shape, dependency, RBAC, or IaC change.

## Validation evidence

- `uv run pytest -q api/tests/test_service_bus*.py api/tests/test_servicebus*.py api/tests/test_resident_consumer.py`
  - 221 passed.
- `uv run pytest -q api/tests`
  - 4852 passed, 3 skipped.
- `uv run ruff check api`
  - Clean.
- `uv run python scripts/docs/check_frontmatter.py`
  - 60 navigated pages checked.
- `DISABLE_MKDOCS_2_WARNING=true uv run mkdocs build --strict`
  - Documentation built successfully.
- `git diff --check`
  - Clean.
