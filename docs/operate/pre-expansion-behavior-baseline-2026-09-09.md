---
title: Pre-expansion behavior baseline (2026-09-09)
description: Immutable source, API, Service Bus, broker, and runtime observations captured before the additive feature expansion.
tags: [operate, architecture, blast]
---

# Pre-expansion behavior baseline (2026-09-09)

## Purpose

This document records the observable behavior that existed before the additive
feature expansion began. It is the regression baseline for reproducibility
packages, shard details, result comparison, OpenAPI contract checks, estimates,
and saved submit templates.

The expansion must not change existing submit, status, result, authentication,
or [Azure Service Bus](https://learn.microsoft.com/azure/service-bus-messaging/service-bus-messaging-overview)
request/completion behavior when the new surfaces are unused. New APIs must be
additive, new persisted fields must be optional, and new UI must be hidden or
inert when its feature is unavailable.

## Source baseline

| Item | Captured value |
| --- | --- |
| UTC observation window | 2026-09-09 03:00-03:03 UTC |
| Branch | `main` |
| Source commit | `476a12014820c75f8d276475bbdd64f1e866345b` |
| `origin/main...HEAD` | `0 0` |
| Worktree | clean |
| Local API | `http://127.0.0.1:8085`, health `200`, revision `local` |
| Local telemetry | Application Insights not configured |
| Local processes | Redis, terminal-exec, API, worker, beat, and web running |

The selected Azure subscription alias was
`ME-MngEnvMCAP132261-moonchoi-1`. Subscription and tenant identifiers and the
signed-in principal are intentionally omitted from this checked-in record.

## Deployed runtime observation

The selected subscription contained the existing resource group
`rg-elb-dashboard`, Container App `ca-elb-dashboard`, and Service Bus namespace
`sb-elb-dashboard-krc` in Korea Central.

At 03:02 UTC, the active Container App revision was
`ca-elb-dashboard--0000635` with one replica and 100 percent traffic. ARM
reported the revision health as `Healthy` but its running state as `Activating`.
The API, frontend, worker, beat, and terminal containers were `Waiting`; Redis
was `Running`. A public `/api/health` request timed out and Container App exec
returned a platform `ClusterExecFailure`. This condition predates the feature
work and must not be attributed to a later code change without a new comparison.

The deployed API/worker image digest at capture time was:

```text
sha256:d0680bedea0d6d6dc5dd3e30609a4905f8258bd9762c2084a498fc07f7e1b466
```

### Deployed Service Bus environment

The active Container App template exposed these non-secret values:

| Container | Variable | Value |
| --- | --- | --- |
| API | `SERVICEBUS_ENABLED` | `true` |
| worker | `SERVICEBUS_ENABLED` | `true` |
| worker | `SERVICEBUS_RESIDENT_CONSUMER` | `false` |
| beat | `SERVICEBUS_ENABLED` | `true` |
| beat | `CELERY_BEAT_SERVICEBUS_DRAIN_SECONDS` | `10` |
| beat | `CELERY_BEAT_SERVICEBUS_PUBLISH_SECONDS` | `10` |

This differs from the source defaults in `infra/control-plane-env.json`, where
the resident consumer is enabled, drain concurrency is four with atomic claim
and single-flight enabled, and the beat fallback drain interval is 60 seconds.
Feature work must preserve both the source contract and this observed deployment
drift; it must not silently rewrite the live Service Bus settings.

## Broker snapshot

Only Azure Resource Manager metadata and count APIs were used. No receive,
peek-lock, send, purge, settlement, configuration write, or entity update was
performed.

### Request queue

Namespace: `sb-elb-dashboard-krc`

Queue: `elastic-blast-requests`

| Property | Value |
| --- | --- |
| Status | `Active` |
| Active messages | `0` |
| Scheduled messages | `0` |
| Dead-letter messages | `0` |
| Transfer dead-letter messages | `0` |
| Lock duration | 1 minute |
| Maximum delivery count | 10 |
| Sessions | disabled |
| Duplicate detection | disabled |
| Entity default TTL | broker maximum |

The request counts remained zero before and after baseline tests.

### Completion topic

Topic: `elastic-blast-completions`

Subscription: `default`

| Property | Value |
| --- | --- |
| Topic status | `Active` |
| Subscription status | `Active` |
| Active messages at first observation | `253` |
| Active messages at final observation | `260` |
| Dead-letter messages | `0` |
| Transfer dead-letter messages | `0` |
| Lock duration | 1 minute |
| Maximum delivery count | 10 |
| Dead-letter on expiration | disabled |

The increase from 253 to 260 occurred without this session publishing or
consuming any event. It is live producer traffic and must be preserved. Future
validation must snapshot this count before and after any Azure test and must not
use the shared deployment-wide configuration row as a test target.

## Local Service Bus default

The local host-mode API had no saved namespace and returned:

```json
{
  "config": {
    "enabled": false,
    "auth_mode": "entra",
    "namespace_fqdn": "",
    "request_queue": "elastic-blast-requests",
    "completion_topic": "elastic-blast-completions",
    "completion_kind": "topic",
    "dlq_cleanup_enabled": false,
    "dlq_max_age_days": 7,
    "dlq_max_count": 5000,
    "dlq_cleanup_batch": 500
  },
  "effective_enabled": false,
  "env_gate_enabled": false,
  "kill_switch_enabled": false,
  "counts": {
    "available": false,
    "reason": "disabled"
  }
}
```

This local disabled state is not evidence that the deployed integration is
disabled.

## Service Bus application contract

### Configuration

- `GET /api/settings/service-bus` returns only `ServiceBusConfig.public_dict()`,
  effective/env/kill-switch gate state, and best-effort entity counts. It never
  returns credentials.
- `PUT /api/settings/service-bus` uses an opaque persisted revision and returns
  `409 servicebus_config_changed` for stale writers.
- Configuration writes take the deployment-wide mutation lock. Routing changes
  also take the request-queue stop-intent and reject while either current or
  proposed request/DLQ backlog, active bridges, or completion outbox entries
  remain.
- Environment-pinned queue/topic/kind values affect runtime only and are not
  copied into the durable settings row.

### Request production

- `POST /api/settings/service-bus/send` supports a pure `dry_run` validation path
  that never touches the broker.
- Real sends require both the deployment gate and saved config to be enabled.
- The route validates the same Pydantic request model used by the consumer,
  rejects target mismatches, enforces the queue-depth ceiling, and rejects the
  wire payload above 192 KiB before broker I/O.
- The request body wire format is the legacy
  `json.dumps(body, default=str)` representation. It must not be normalized or
  augmented implicitly.
- `external_correlation_id` is the idempotency identity. `request_id` is an
  optional pass-through tracking value.
- An ambiguous send returns `send_outcome_unknown` and the reusable correlation
  id. A confirmed send creates a best-effort queued placeholder and invalidates
  job-list caches; neither follow-up may turn a confirmed broker send into an
  HTTP failure.

### Request consumption and settlement

- Service Bus tasks are routed to the dedicated `servicebus` worker queue.
- The resident consumer and periodic Celery fallback are separate execution
  modes. The observed deployment has resident mode off and a 10-second fallback
  tick.
- One fallback task has a 40-second work budget, reserves 5 seconds for
  settlement, and uses at most a 35-second sibling submit timeout with zero
  inline transport retry and zero token resync.
- A drain pass is bounded. Atomic correlation claims prevent duplicate submits,
  and the queue-scoped single-flight lease excludes overlapping drains and
  configuration changes.
- `MessageAction.COMPLETE` completes the received message.
- `MessageAction.ABANDON` returns it to the broker.
- `MessageAction.DEAD_LETTER` dead-letters it with a sanitized reason.
- `MessageAction.RETRY` schedules a retry clone before completing the original.
  If scheduling or settlement fails, the original is abandoned so work is not
  lost.
- Every broad exception boundary in the Service Bus path must re-raise Celery
  `SoftTimeLimitExceeded`.

### Completion publication

- Completion payloads are capped at 192 KiB.
- The completion entity defaults to a topic; queue mode is supported explicitly.
- Event subjects come from the event type, correlation id is copied to the
  broker envelope, and a caller `request_id` is copied to application
  properties when present.
- The producer protocol is two-phase: durable queued acceptance first, then a
  terminal succeeded/failed event. An optional running transition may appear.
- Responses are persisted to the durable outbox before best-effort publication.
  A publish failure defers the row; it must not drop or reorder another
  correlation's events.
- Duplicate requests replay their queued acknowledgement without creating a
  second BLAST execution.

### Operator and observation routes

The current OpenAPI surface contains these Service Bus routes:

```text
GET,PUT /api/settings/service-bus
POST    /api/settings/service-bus/test
POST    /api/settings/service-bus/discover
POST    /api/settings/service-bus/purge
POST    /api/settings/service-bus/send
GET     /api/settings/service-bus/peek
GET     /api/settings/service-bus/dlq/peek
POST    /api/settings/service-bus/dlq/delete
POST    /api/settings/service-bus/dlq/promote
GET     /api/settings/service-bus/observed-completions
POST    /api/settings/service-bus/drain
```

Peek routes are non-destructive and always degrade to an unavailable payload.
DLQ delete/promote and purge are bounded operator mutations. Completion
observations are read from the worker-side Redis ring and do not consume the
external subscription.

## Existing BLAST feature surface

Before expansion, the following behaviors already exist:

- Job list/create, detail/delete, cancel/retry, execution steps, events, query,
  queue information, result files, aggregate results, server-side alignment
  pagination/filtering, taxonomy rollups, result export, and streamed download.
- Persisted canonical submit snapshots and provenance bundles.
- Text, Markdown, and BibTeX Methods citations.
- Nextflow, Snakemake, CWL, and WDL workflow exports.
- Split-query child rows and an aggregate `split_children` summary on parent
  projections.
- Cluster-level approximate cost estimation and a per-cluster budget warning.
- Light, dark, and system themes with immediate theme switching.

The following requested surfaces do not exist at this baseline:

- A single reproducibility package endpoint/download.
- A dedicated per-shard detail endpoint and per-shard result view.
- A read-only comparison endpoint for two completed jobs.
- A checked-in OpenAPI compatibility snapshot or generated TypeScript model
  namespace.
- A calibrated BLAST runtime estimate on the submit form. The existing
  `/api/blast/cost-estimate` route is a `503` lab-tool stub.
- Owner-scoped saved submit templates.

## Protected-file fingerprints

The following SHA-256 values identify the request queue, completion topic,
tracking, outbox, configuration, and Message Flow implementation at this
baseline:

```text
e231bc57d25b59b7b49bc581d86823d338a7a02a628576f529878816edb54388  api/services/service_bus.py
a2baa9ba2a76cd4070985175d8be4c386377f12e31111129dd35f8c0517f3e23  api/services/service_bus_pref.py
84958c2dd30cd82550325c4bf42d65369c5a921799d8ddbea8cf110552582361  api/services/service_bus_tracking.py
d8afc59769c3aaeba69088bd3b87df88239895474643c75892510b95fbfe32e8  api/services/service_bus_outbox.py
523babb7aeec5568f047058684ee66bdd2e4c0fb93f6096b11ab782ae1cf71a0  api/tasks/servicebus/tasks.py
7a34d04a58afb8eb1b48a6e2e044562253bc51cd747387e4c9d0c048d79da6e0  api/tasks/servicebus/drain_coordination.py
ea43b31bccb677b98b4c655d871e157d4621f41cff24d417806ed256b5519491  api/routes/settings/service_bus.py
be7b0b629bddb7d2d55aac138978414289e53146fbd812a93f11ec568a55e2a3  web/src/components/cards/MessageFlow/MessageFlowCard.tsx
a53a833d172543bbf3a5e52c46b7a586b18e4c52c60753fe643263ad717b27e5  web/src/components/cards/MessageFlow/serviceBusTelemetryFormat.ts
```

Changing one of these files requires an explicit Service Bus design review and
a before/after broker contract test. The planned additive features do not
require such a change.

## Baseline validation

```text
uv run pytest -q api/tests/test_service_bus*.py api/tests/test_servicebus*.py
354 passed

npm --prefix web test -- --run \
  src/components/cards/MessageFlow/serviceBusTelemetryFormat.test.ts \
  src/components/cards/MessageFlow/constellationModel.test.ts \
  src/pages/BlastJobs/jobSource.test.ts \
  src/pages/blastResults/configFormat.test.ts
4 files passed, 43 tests passed
```

The local API health endpoint returned `200`. The deployed health endpoint
condition is recorded separately above and was not used to claim a green
production baseline.

## Required comparison after each feature slice

1. Re-run the 354 Service Bus backend tests and 43 frontend display tests.
2. Verify the protected-file hashes are unchanged, or stop for an explicit
   Service Bus review.
3. Compare request queue active/scheduled/DLQ/transfer-DLQ counts before and
   after any Azure validation. Do not send or receive a test request unless the
   scenario explicitly requires it.
4. Never repoint the deployment-wide Service Bus settings row for testing.
5. Never purge or consume the shared completion subscription. Treat its active
   count as externally owned traffic.
6. Verify all new routes use `require_caller`, return additive response shapes,
   and do not mutate existing job rows unless the feature explicitly owns a new
   opt-in action.
7. Run the full backend and frontend suites, Ruff, the mypy debt ratchet, and the
   production frontend build before completion.
