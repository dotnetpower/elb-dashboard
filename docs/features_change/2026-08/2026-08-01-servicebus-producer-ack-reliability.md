---
title: Service Bus producer ACK reliability and worker isolation
description: Accept WF3 correlation ids with spaces, preserve specific DLQ causes, prevent delivery-count burn, order producer responses, and isolate ACK work from slow reconciliation.
tags:
  - blast
  - architecture
  - operate
---

# Service Bus producer ACK reliability and worker isolation

## Motivation

A customer WF3 producer observed frequent Phase 1 ACK timeouts. Read-only
analysis of [Application Insights](https://learn.microsoft.com/azure/azure-monitor/app/app-insights-overview)
and Container App logs for revision `ca-elb-dashboard--0000251` found two
independent failure classes:

1. Production correlation ids embed human-readable gene names. Multi-word names
   such as `hypothetical protein` failed the dashboard's former no-space regex
   before OpenAPI submission and were dead-lettered under the generic
   `handler_rejected` reason.
2. Service Bus periodic work shared the single-concurrency `reconcile` Celery
   worker with runtime-metrics backfill. From 2026-07-30 18:18 UTC through
   2026-08-01 02:08 UTC, 15,311 drain ticks, 7,100 transition ticks, 1,062 DLQ
   reconcile ticks, and 208 health ticks expired before execution. Runtime
   backfill averaged 204 seconds, reached 3,303 seconds, and ran every five
   minutes. This delayed ACK outbox publication independently of BLAST capacity.

The same window recorded 209 accepted requests, 97 claim/admission deferrals,
25 duplicate ACK replays, and 130 abandoned settlements. Request custom events
were visible as console records, but the resident consumer ran in a Celery
parent without an initialized exporter, so the correlation dimensions did not
reach `AppEvents`.

## User-facing change

- WF3 `external_correlation_id` values may contain spaces. Unsafe Azure Table
  row keys use a stable SHA-256 key, avoiding collisions with underscore forms.
- Handler dead letters expose their machine reason instead of the generic
  `handler_rejected` fallback.
- Claim contention, a post-receive admission close, and response-outbox
  unavailability use expiry-preserving scheduled retries. They no longer burn
  `max_delivery_count` merely because the resident loop polls frequently.
- A bridge does not poll or stage a later status while an older response for the
  same correlation remains in the durable outbox. Producer events preserve
  `queued -> running -> terminal` ordering.
- Service Bus periodic tasks use a dedicated `servicebus` Celery queue and
  `worker-servicebus` process. General reconciliation and runtime backfill
  cannot starve ACK/outbox delivery.
- Runtime-metrics backfill runs hourly in five-row passes, uses early ACK, and
  does not poison-redeliver after worker loss.
- The Service Bus parent initializes Azure Monitor after prefork. Request
  dimensions reach `AppEvents`; a payload-free searchable console line remains
  as startup fallback.

## API, task, and infrastructure summary

- Request models accept spaces in correlation ids while retaining the 256-byte
  bound and restricted printable character set.
- Bridge persistence hashes only Table-unsafe ids; existing safe RowKeys are
  unchanged.
- `ParsedMessage` carries a bounded settlement reason and description consumed
  by the Service Bus dead-letter operation.
- The response outbox stores correlation ids and builds one bounded pending
  snapshot per transition tick. It does not issue one unindexed Azure Table
  query per active bridge.
- Celery routing adds an internal `servicebus` queue and parent process without
  adding or repointing any Azure resource. Total prefork child concurrency
  remains five (`2 + 1 + 1 + 1`).
- No Service Bus namespace, queue, topic, subscription, Storage account, or AKS
  resource is created or changed by this code change.

## Validation evidence

- Customer log queries against the existing Log Analytics workspaces; no shared
  settings or resources were mutated during diagnosis.
- Focused request, drain, outbox, tracking, telemetry, queue-isolation, external
  API, and BLAST task tests.
- Full backend test suite and Ruff lint.
- Documentation frontmatter and strict MkDocs build.
- Customer-environment post-deploy validation must confirm:
  - `worker-servicebus` starts and consumes the dedicated queue;
  - new `servicebus_request` rows include correlation/action/ACK dimensions;
  - periodic Service Bus task expiry remains zero under runtime backfill;
  - a WF3-style multi-word correlation receives a queued ACK and one terminal
    event in order;
  - request and completion DLQs remain empty for the validation window.
