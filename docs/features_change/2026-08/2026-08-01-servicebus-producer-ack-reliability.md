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
- Transition polling runs every 30 seconds in 20-bridge pages with a 60-second
  hard deadline. A live first-revision sweep of 200 legacy bridges took 240
  seconds and expired queued periodic ticks; the bounded page processes that
  backlog fairly without monopolizing the dedicated worker.
- The resident-primary deployed policy runs the beat drain fallback every 60
  seconds instead of every 5 seconds. Live admission probes reached 45 seconds;
  the old cadence queued already-obsolete fallbacks even though the resident
  parent continued accepting requests. The fallback interval now exceeds its
  bounded execution window while retaining recovery if the resident loop exits.
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
- Customer revision `ca-elb-dashboard--0000257` started
  `worker-servicebus` plus the resident consumer. Revision
  `ca-elb-dashboard--0000258` applied the 60-second fallback policy and reached
  Healthy / RunningAtMaxScale at 100% traffic.
- Four prepared `core_nt` requests used correlation ids containing the exact
  multi-word gene-name patterns seen in production (`B22R family protein`,
  `hypothetical protein`, `DNA-dependent RNA polymerase subunit rpo132`, and
  `RNA polymerase subunit RPO18`). Application Insights recorded 4/4
  `accepted`, 4/4 `ack_published=true`, delivery count 0, and ACK latencies of
  14.672, 24.242, 18.310, and 29.203 seconds.
- Their OpenAPI job ids (`2b144118d0fa`, `b09779765d01`, `c026a8d3387d`, and
  `f651c9cfd9ba`) all reached Completed in the dashboard. Transition publisher
  ticks emitted terminal outcomes with `errors=0`.
- A separate 16S negative control proved Phase 1 independently: its multi-word
  correlation received `ack_published=true` before the job failed in Phase 2
  because that database was not prepared in the customer cluster.
- The bounded live publisher processed 20 bridges in 13.8 and 26.7 seconds,
  compared with 240 seconds for the original 200-row pass.
- After the final fallback policy took effect, the sampled final-revision
  window showed drain, transition, and DLQ reconcile tasks received and
  succeeded with zero revoked Service Bus tasks. The Playground reported
  request queue 0 and DLQ 0.
- Application Insights contained no Service Bus worker exceptions or
  error-level Service Bus traces in the final validation window.
