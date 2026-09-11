---
title: Bound Service Bus execution admission
description: Replace the growing warmup JobState scan with durable key-addressable markers and stop admission work before the Celery settlement reserve.
tags:
  - blast
  - architecture
  - operate
---

# Bound Service Bus execution admission

## Motivation

A production `drain_and_resubmit` tick spent about 43 seconds reading nine sequential pages from
the JobState table before it could decide whether a warmup was active. The query filtered on
non-key `type` and `status` properties, so [Azure Table
Storage](https://learn.microsoft.com/azure/storage/tables/table-storage-overview) scanned a growing
table instead of using a key range. The 45-second [Celery](https://docs.celeryq.dev/) soft limit
then interrupted the [Service Bus](https://learn.microsoft.com/azure/service-bus-messaging/service-bus-messaging-overview)
drain before it opened the receiver.

Execution admission also had no caller deadline. A slow readiness dependency could consume the
whole task budget even after the drain had reserved time for broker settlement.

## User-facing change

- Service Bus requests remain queued while a manual or automatic database warmup is queued or
  running.
- Admission no longer scans all historical JobState rows, so queue draining does not slow down as
  job history grows.
- A periodic drain that runs out of its admission budget defers the tick without receiving or
  consuming a request delivery attempt.
- Warmups that hand node-readiness waiting back to the recurring reconciler release their active
  marker immediately; the live warmup readiness gate remains closed until the replacement task is
  ready to run.

## API and runtime changes

- Warmup producers create one durable, per-job marker under a cluster-scoped RowKey prefix before
  enqueueing the task. Enqueue failures terminalise the JobState row and remove the marker.
- Admission lists at most 256 marker rows through an indexed `PartitionKey + RowKey` range, then
  performs an exact projected JobState lookup for those job IDs only.
- Deployed reads are strict: a Table outage or malformed marker fails closed instead of being
  interpreted as an empty active set. Local API and worker processes share markers through Redis.
- Terminal warmup tasks and completed defer handoffs remove their marker. Admission also cleans
  markers whose JobState is terminal and ages out missing-row orphans after the existing bounded
  stale interval.
- The Service Bus fallback passes its monotonic deadline through initial and pre-submit admission.
  State/dependency boundaries stop when the caller budget is exhausted, budget denials are never
  cached, and ten seconds are reserved around admission and successful-submit persistence.
- No route, response schema, infrastructure resource, role assignment, public network setting, or
  browser data-plane contract changes.

## Hardening review

Fourteen adversarial review rounds were applied until no reproducible Medium-or-higher finding
remained:

1. Indexed access: replaced the non-key JobState scan with a cluster-prefix RowKey query.
2. Cardinality: capped one admission read at 256 markers and fail closed on overflow.
3. Cold start: strict reads ensure the singleton table before querying it.
4. Query integrity: escaped OData string literals and tested quote-containing generic prefixes.
5. Write ordering: persisted JobState and admission marker before the broker side effect.
6. Partial failure: marker persistence failure prevents enqueue; enqueue failure terminalises and
   rolls back.
7. Concurrency: independent job keys preserve simultaneous warmups without read-modify-write loss.
8. Scope isolation: payload scope and derived key must match the exact subscription, resource group,
   cluster, and job identity.
9. Cross-process behavior: local API and worker processes merge Redis and in-process marker views.
10. Terminal lifecycle: completed, failed, cancelled, deleted, and reconciler-deferred tasks cannot
    leave an active marker blocking later execution.
11. Deadline propagation: initial and pre-submit admission share the caller's monotonic work
    deadline.
12. Cache semantics: caller-budget denials retain their own reason and are never cached across
    ticks.
13. Settlement safety: OpenAPI readiness overruns map to a task-budget defer, claims are released,
    and ten seconds remain for post-submit durable state and broker settlement.
14. Compatibility/security: existing call sites retain optional keyword defaults; no auth, SAS,
    RBAC, Storage network, or terminal boundary changed.

Rejected review suggestions included enqueue-before-marker ordering, which would create a real
fail-open window where warmup work could start without admission state, and truncating an overflowed
marker set, which would hide active work. Both conditions remain deliberately fail closed.

## Validation

- Focused marker, admission, warmup route/reconciler, Service Bus task, and load suites: 282 passed.
- Post-hardening lifecycle/admission sweep: 234 passed.
- Hermetic full backend suite: 5,936 passed, 4 skipped because optional XML parity evidence was
  not configured. The eight failures from the first run all passed in isolation after clearing
  live-deployment environment variables inherited by the reused terminal.
- Slow/subprocess backend suite: 160 passed.
- Full frontend suite: 1,036 passed across 118 files; ESLint and the production build passed.
- Ruff lint and format, the production mypy debt ratchet, OpenAPI contract/generated types, docs
  frontmatter, and strict MkDocs build passed.
- Local detached fullstack smoke passed 27/27 routes. Expected degraded-path logs came only from
  the smoke fixture's all-zero subscription and unavailable local Storage data-plane permissions;
  no admission timeout or `SoftTimeLimitExceeded` was emitted.

Full CI-parity and live rollout evidence will be appended after the API, worker, and beat sidecars
converge. The rollout reuses existing infrastructure and does not create or repoint an Azure
resource. Tier 2a host-mode smoke passed, but its local file/Redis state backend cannot reproduce
the production Azure Table's accumulated pagination and network latency. Live validation is
therefore limited to the API/worker/beat image and passive Service Bus/App Insights telemetry; the
frontend, terminal, OpenAPI workload, sidecar layout, and infrastructure remain unchanged.
