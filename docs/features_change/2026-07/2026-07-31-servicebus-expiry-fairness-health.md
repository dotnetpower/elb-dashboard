---
title: Service Bus expiry, fair transition polling, and policy health
description: Preserve request expiry through retries, prevent transition starvation and outbox head-of-line blocking, and expose broker policy plus warmup admission health.
tags:
  - blast
  - architecture
  - operate
---

# Service Bus expiry, fair transition polling, and policy health

## Motivation

Producer ACK timeouts, broker message retention, AKS/database warmup admission,
and terminal result delivery are separate clocks. The control plane already
kept requests broker-owned during warmup and persisted producer responses in a
durable outbox, but scheduled retries could receive a fresh broker lifetime,
the transition publisher repeatedly selected only the first bounded bridge
page, and one permanently oversized response could stop unrelated outbox
delivery. Operators also could not verify the live entity TTL/dead-letter
policy or see bounded warmup counts in health telemetry.

## User-facing change

- Dashboard-origin requests carry an explicit 24-hour Service Bus TTL. Delayed
  retry clones preserve the original absolute expiry; a retry that would start
  after expiry produces `servicebus_request_expired` instead.
- Request queue and completion subscription TTL, dead-letter-on-expiration, and
  max-delivery policy are included in the payload-free `servicebus_health`
  snapshot. Unsafe or unavailable policy emits bounded warning codes.
- Health telemetry includes target/Ready node counts and pending/failed warmup
  job counts without database names or Job IDs.
- Active bridge polling uses a revision-local Redis keyset cursor (with a
  process fallback), so more than 200 active jobs are visited fairly instead of
  starving later rows. A revision restart may begin again at the first page;
  durable bridge status markers make that re-poll idempotent.
- A deterministically oversized completion response is replaced under the same
  `event_id` by a bounded claim-check event (optional `result_files` and verbose
  detail are dropped while `result_ref` remains). It blocks only later events
  for the same correlation until the compact event publishes. Unrelated
  producer responses continue; entity-wide broker/auth/network failure still
  stops the pass immediately to avoid an outage retry storm.
- The architecture guide now separates Phase 0 broker acceptance, Phase 1
  execution acceptance, and Phase 2 terminal outcome, including late-event and
  warmup behavior.

## API, task, and infrastructure summary

- `api/services/service_bus.py`: explicit producer TTL, expiry-preserving retry
  clones, completion wire-size validation, and static entity policy projection.
- `api/services/service_bus_tracking.py`: bounded RowKey-cursor active pages.
- `api/services/service_bus_outbox.py`: durable failure count and next-attempt
  metadata.
- `api/services/service_bus_health.py` and task observability: policy and warmup
  scalar dimensions plus unsafe-policy/poison-response warnings.
- `infra/control-plane-env.json` and the Container App template set
  `SERVICEBUS_REQUEST_TTL_SECONDS=86400` for the API producer. No Azure role,
  network rule, entity, or managed resource is added or changed.

## Validation evidence

- Focused Service Bus, admission, outbox, tracking, and observability tests.
- Full backend suite and Ruff lint.
- The Container App module and subscription entry-point Bicep compiled to
  temporary ARM JSON, and both outputs contained the TTL environment binding.
- Documentation frontmatter and strict MkDocs build.
- Customer-environment validation remains required for live queue/subscription
  policy, warmup delay, listener catch-up, broker expiry/DLQ, and 201+ active
  bridge behavior.