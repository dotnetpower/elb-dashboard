---
title: Durable Service Bus responses and lifecycle-safe retries
description: Preserve requests across long database lifecycle work and guarantee durable producer outcomes for accepted, failed, expired, and operator-removed requests.
tags:
  - blast
  - architecture
  - operate
---

# Durable Service Bus responses and lifecycle-safe retries

## Motivation

[Azure Service Bus](https://learn.microsoft.com/azure/service-bus-messaging/service-bus-messaging-overview)
requests can wait while [AKS](https://learn.microsoft.com/azure/aks/what-is-aks)
starts or while a large BLAST database update and node-local warmup run for more
than an hour. The previous drain correctly deferred receive during admission,
but transient submit failures used immediate abandon, large receive batches
could outlive their lock budget, and completion publish failure was coupled to
request settlement. TTL or max-delivery DLQ outcomes also had no mandatory
producer response.

## User-facing change

- Database update, warmup, start, and scale admission still happens before
  receive. A multi-hour blocked lifecycle does not increment message delivery
  count or consume application retry attempts.
- Drain batches are capped to handler concurrency and each pass has a wall-clock
  budget. An active batch finishes; untouched backlog stays in Service Bus for
  the next resident/beat pass instead of timing out the Celery task.
- Transient OpenAPI 408/429/5xx and transport failures become future-scheduled
  retries with bounded exponential backoff. Scheduling succeeds before the
  original message completes; scheduling failure preserves the original.
- Every producer transition is persisted to a durable Azure Table outbox before
  terminal request settlement or bridge completion. Completion publish is
  at-least-once and uses the stable `event_id` for consumer de-duplication.
- Retry exhaustion, queue TTL expiry, max-delivery exhaustion, correlation
  conflict, malformed requests, permanent OpenAPI rejection, lifecycle
  interruption, bridge timeout, and operator purge all produce a terminal
  `failed` response. DLQ messages are removed only after response persistence
  and audit backup both succeed.
- Queue-arrival auto-start now uses the same gate in API and worker sidecars.
  Worker-side pending-queue reconciliation covers producers that send directly
  to the namespace and repeatedly observes `Stopping` until a settled cluster
  can start.
- A queue-scoped Redis stop-intent fence is atomically exclusive with the drain
  lease. Auto-stop rechecks queue depth after fencing, while each claimed
  request rechecks lifecycle admission immediately before submit. This closes
  both stop-versus-PEEK_LOCK and receive-versus-new-barrier races.

## API, task, and infrastructure summary

- `api/services/service_bus_outbox.py` adds idempotent event-id keyed response
  persistence with deployed fail-closed Table requirements.
- `api/services/service_bus.py` adds concurrency-sized receive batches, bounded
  passes, delayed retry settlement, DLQ response draining, and response-first
  operator purge/delete callbacks.
- `api/tasks/servicebus/tasks.py` stages accepted and terminal responses,
  flushes the outbox independently of OpenAPI readiness, and reconciles DLQ
  terminal outcomes on a dedicated beat task.
- `api/services/aks/queue_autostart.py` adds a debounced pending-queue trigger.
- `infra/control-plane-env.json` and the Container App module pin the retry,
  lock, pass, and API/worker auto-start values. No Azure role, network rule,
  public endpoint, or new managed resource is introduced; the outbox uses the
  existing platform Storage account.

## Validation evidence

- Reliability acceptance suite: `107 passed`, covering concurrency-sized
  batches, pass-budget yield, scheduled retry, two-hour admission deferral,
  retry exhaustion, TTL DLQ response, and audit backup.
- Outbox/task focused suite: `82 passed`.
- DLQ/operator response-first suite: `136 passed`.
- Control-plane env, auto-start, and Celery schedule guards: `35 passed`.
- Full backend suite: `4879 passed, 3 skipped`; full Ruff lint passed.
- Stop/drain fence, pre-submit admission, load, and auto-start focused suite:
  `109 passed`; strict mypy passed for all five changed core modules.
- Ruff lint and Python compilation passed on all touched backend modules.
- Host-mode local smoke passed for `/api/health`, the SPA reverse proxy, and
  `/api/settings/service-bus`; validation services were stopped afterwards.
- Bicep module and subscription entry point compiled locally. No deployment or
  Azure resource mutation was performed. Subscription what-if was not run
  because the customer Azure CLI profile requires Conditional Access
  re-authentication; the generated ARM templates and JSON/Bicep parity guards
  passed locally.