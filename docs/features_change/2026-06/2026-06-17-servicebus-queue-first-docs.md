---
title: Clarify Service Bus queue-first architecture docs
description: Service Bus docs now describe the request queue as the required submit path and the completion topic as an optional push channel, matching the current queue-heavy runtime contract.
tags:
  - blast
  - architecture
---

# Clarify Service Bus queue-first architecture docs

## Motivation

The public Service Bus architecture page still read as if the completion topic
were a mandatory part of the submit path. The runtime contract is queue-first:
all BLAST submissions enter through the `elastic-blast-requests` queue, while
the completion topic is an optional fan-out channel for deployments that want
push notifications.

## User-facing change

Documentation now distinguishes the required request queue from the optional
completion topic. External systems can always poll status/results by correlation
id or job id; deployments that configure the completion topic can additionally
subscribe to `blast.transition` events.

## API / IaC diff summary

- Documentation-only clarification in the Service Bus architecture page,
  examples page / README, and related feature-change notes.
- The standalone example consumer/monitor now read `SERVICEBUS_RESPONSE_TOPIC`
  first and keep `SERVICEBUS_COMPLETION_TOPIC` as a compatibility alias.
- No runtime API, worker, or infrastructure change for the queue-first docs.

## Validation evidence

- Source checked against `api.services.service_bus.publish_event`,
  `api.tasks.servicebus.publish_transitions`, and `ServiceBusConfig`: the
  request queue is mandatory for submit ingestion, and `completion_topic` is an
  optional push channel.