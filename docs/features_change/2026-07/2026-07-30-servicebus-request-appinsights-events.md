---
title: Service Bus request lifecycle events in Application Insights
description: Add payload-free per-request custom events for Service Bus enqueue, acceptance, retry ACK, conflict, rejection, abandonment, and deferral decisions.
tags:
  - blast
  - operate
  - architecture
---

# Service Bus request lifecycle events in Application Insights

## Motivation

The Service Bus drain already wrote aggregate non-empty tick summaries and
warning/error lines through the Python logger. When server-side
[Application Insights](https://learn.microsoft.com/azure/azure-monitor/app/app-insights-overview)
was configured, those records reached `traces`, but an operator could not query
one consistent structured event to follow an individual request through enqueue,
deduplication, ACK replay, conflict handling, and settlement decisions.

## User-facing change

The control plane now emits a `servicebus_request` custom event for:

- `enqueued` and `enqueue_failed` producer outcomes;
- `accepted` first submissions, including whether the queued ACK was published;
- `retry_ack_replayed` exact retries;
- `correlation_conflict`, `rejected`, `abandoned`, and `deferred` drain decisions.

Dimensions are limited and length-bounded: correlation/request/message/OpenAPI
job ids, queue, program, database, taxid direction, delivery/sequence counters,
settlement action, ACK flag, and error code. Query FASTA, BLAST options, raw
message bodies, credentials, and completion payloads are excluded by the
helper's explicit function signature.

Telemetry remains opt-in. Without a server Application Insights connection
string, the same best-effort event is only a local structured log line and does
not create an Azure dependency or alter queue processing.

## API / IaC diff summary

- `api/services/service_bus_observability.py`: scalar-only event shaping.
- `api/services/service_bus.py`: enqueue success/failure events.
- `api/tasks/servicebus/tasks.py`: drain decision events.
- No route, response schema, dependency, RBAC, IaC, or deployment change.

## Validation evidence

- `uv run pytest -q api/tests/test_service_bus*.py api/tests/test_servicebus*.py api/tests/test_resident_consumer.py`
  - 226 passed.
- `uv run pytest -q api/tests`
  - 4858 passed, 3 skipped.
- `uv run ruff check api`
  - Clean.
- `uv run python scripts/docs/check_frontmatter.py`
  - 60 navigated pages checked.
- `DISABLE_MKDOCS_2_WARNING=true uv run mkdocs build --strict`
  - Documentation built successfully.
- Tests cover event stages, enqueue failure re-raise, ACK/conflict behavior,
  fail-safe emission, scalar bounds, and payload exclusion.