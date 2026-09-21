---
title: Recover Service Bus queue projections after revision changes
description: Completion publication now terminalizes durable job rows, while strict Kubernetes evidence releases queues blocked by a lost warmup task result.
tags:
  - blast
  - operate
  - architecture
---

# Recover Service Bus queue projections after revision changes

## Motivation

The Message Flow modal showed dozens of queued requests even though Azure
Service Bus held only a small, bounded number of messages. The displayed boxes
were durable JobState rows whose message-flow history had already reached
`mf.succeeded` and `mf.completion_published`; the completion publisher recorded
history and timing but left the indexed top-level status as `queued`.

A Container App revision change also removed an auto-warmup task's ephemeral
Celery result after its Kubernetes Jobs had completed. The durable warmup row
remained `running`, so execution admission preserved new Service Bus requests
instead of draining them.

## User-Facing Change

- Message Flow removes completed or failed Service Bus jobs as soon as their
  durable completion response is staged.
- Existing stale rows converge from durable completion-publication evidence on
  the periodic BLAST reconciler.
- A strict all-node warmup whose Celery result was lost can recover immediately
  once Kubernetes proves every expected node Ready for one source generation.
- Requests remain in Service Bus while admission is closed; no purge or
  receive-before-readiness path was added.

## API / IaC Diff Summary

- The Service Bus transition publisher maps `running`, `succeeded`, and
  `failed` transitions onto the corresponding indexed JobState status/phase.
- The stale BLAST reconciler accepts only `submission_source=servicebus` plus
  `completion_published_at` and exactly one success/failure timestamp as
  terminal evidence.
- Warmup recovery requires phase `warming_nodes`, Celery `PENDING`, strict
  all-node mode, the expected Ready job count, zero active/failed jobs, a
  `warmup` source, and exactly one source generation.
- Warmup probes are cached per cluster and capped at eight distinct scopes per
  reconciliation tick. Lifecycle barrier correlations remain intact until
  admission observes the completed row.
- No HTTP schema, Service Bus entity, retry policy, IaC resource, or browser
  authentication contract changed.

## Validation Evidence

- Live Service Bus telemetry showed queue policy TTL 14 days, dead-letter on
  expiration enabled, max delivery count 10, and zero DLQ messages.
- The admission-blocked messages remained visible in the request queue and
  drained to zero after 10/10 Kubernetes warmup Jobs were proven Ready and the
  stale warmup row was recovered.
- A production dry run scanned 500 active BLAST rows and found 499 stale
  Service Bus rows with unambiguous published-success evidence; no broker
  message or row without terminal evidence was selected.
- The requested dashboard job owned by `moonchoi@microsoft.com` reached
  `completed` before cancellation; no active job remained to cancel.
- Focused Service Bus, BLAST reconciler, and stale-dbops tests passed; the full
  backend suite passed with 6,151 tests and 4 fixture-dependent skips.
- Ruff, the production mypy debt ratchet, the 242-operation OpenAPI contract,
  generated TypeScript API types, and strict MkDocs build passed.