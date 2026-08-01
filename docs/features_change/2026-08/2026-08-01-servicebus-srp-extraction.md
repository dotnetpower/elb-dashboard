---
title: Service Bus SRP extraction without contract changes
description: Split Redis drain coordination and read-only Service Bus management projection from the large compatibility facades while preserving task names, imports, and monkeypatch seams.
tags:
  - architecture
  - blast
  - contributor
---

# Service Bus SRP extraction without contract changes

## Motivation

The Service Bus reliability work left two broad modules with unrelated layers:

- `api/tasks/servicebus/tasks.py` combined Redis lease mechanics with Celery task
  orchestration and request/bridge state machines.
- `api/services/service_bus.py` combined message data-plane settlement with
  read-only entity policy, health projection, and resource discovery.

Those mixed responsibilities made later changes risky: a Redis coordination
edit required loading the full BLAST state machine, while an additive health
field touched the message settlement module. The extraction deliberately leaves
the high-risk drain FSM, retry identity, durable outbox boundary, and explicit
Celery task decorators in place.

## User-facing change

There is no API, UI, message-schema, queue, or deployment behavior change.
Existing Service Bus requests, ACKs, completion transitions, and operator
settings use the same public functions and registered Celery task names.

## API and task diff summary

- `api/tasks/servicebus/drain_coordination.py` now owns queue-scoped Redis drain
  leases, compare-and-delete release, and the auto-stop intent fence.
- `api/tasks/servicebus/tasks.py` remains the stable task/state-machine facade.
  Its wrappers inject the facade's current gate, TTL, key constants, and logger
  into the focused module, preserving existing monkeypatch and auto-stop imports.
- `api/services/service_bus_management.py` now owns read-only queue/topic runtime
  projection, static policy telemetry, pending-depth reads, and namespace/entity
  discovery.
- `api/services/service_bus.py` remains the stable data-plane facade. Its wrappers
  inject the current `_admin_client`, config resolver, completion-kind resolver,
  logger, and normalized auth exception, so tests and external callers require
  no import changes.
- Celery names remain `api.tasks.servicebus.*`; no task body moved and no queue
  routing changed.
- No dependency, Azure role, Service Bus entity, Storage schema, or environment
  variable was added.

## Validation evidence

- Focused SRP boundary tests verify facade dependency injection, legacy symbol
  availability, and all five registered Celery names.
- Existing Service Bus task/load, entity-count, health, settings, auto-stop, and
  facade-contract tests cover the extracted implementations through the legacy
  entry points.
- Full backend suite and Ruff lint.
- Documentation frontmatter and strict MkDocs build.
- Consumer search confirms routes, resident consumer, auto-stop, and tests still
  import the original facade symbols.
