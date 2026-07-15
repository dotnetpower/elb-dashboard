---
title: Service Bus lifecycle execution admission
description: Keep BLAST request messages queued while AKS scales, starts, stops, or warms databases, and bound accepted jobs lost by a lifecycle transition.
tags:
  - blast
  - architecture
---

# Service Bus lifecycle execution admission

## Motivation

[Azure Service Bus](https://learn.microsoft.com/azure/service-bus-messaging/service-bus-messaging-overview)
request messages could be consumed while [Azure Kubernetes Service
(AKS)](https://learn.microsoft.com/azure/aks/what-is-aks) was changing the workload-pool
node count or restoring node-local database caches after a start. The periodic drain had a
coarse OpenAPI readiness check, but the resident consumer bypassed it, and OpenAPI readiness
did not prove that the target node count or database warmup had completed. A request could
therefore leave the queue, become `running`, lose its execution pod during the lifecycle event,
and remain active indefinitely.

## User-facing change

- AKS start, scale, stop, and delete requests create a durable per-cluster lifecycle barrier
  before the Celery lifecycle task is enqueued.
- The periodic drain, resident consumer, and per-message submit handler share one strict
  execution-admission decision. While blocked, messages remain in the Service Bus request
  queue and dashboard-owned placeholders remain `queued`.
- Start and scale barriers open only after the ARM lifecycle operation converges, the exact
  target workload-node count is present, every target node is Kubernetes Ready, and every
  configured post-lifecycle database warmup Job is `completed`.
- A failed warmup keeps the request queue closed with reason `database_warmup_failed`; the
  system does not silently fall back to a cold submit. Active manual warmup jobs close the
  same gate even when Auto warm is not configured.
- An already-accepted external job that disappears from both Kubernetes and OpenAPI after a
  newer lifecycle generation is terminalised after the existing stale threshold with
  `error_code=cluster_lifecycle_interrupted`. The matching Service Bus bridge publishes the
  same bounded failure instead of remaining active indefinitely. Start/scale waits until the
  recovered execution plane confirms either that the job is missing, or that OpenAPI is stuck
  active while Kubernetes has no matching Jobs or Pods. A transient offline plane is not treated
  as proof of failure.

## API / task / IaC diff summary

- `api/services/aks/execution_admission_state.py` stores immutable lifecycle generations and
  token-scoped warmup/LRO completion records in the existing durable dashboard singleton
  table, mirrored to Redis for fast cross-sidecar reads. `execution_admission.py` owns only
  strict ARM/Kubernetes/warmup readiness decisions. Deployed reads distinguish a confirmed
  missing row from a Table failure so a storage outage cannot silently open admission.
- `api/routes/aks/lifecycle.py` writes the barrier before enqueue and cancels only that token
  if enqueue fails.
- `api/tasks/azure/lifecycle.py` restores saved Auto warmup preferences for queue-triggered
  starts, records ARM convergence, and forwards the lifecycle token to forced warmup
  reconciliation. Queue-autostart now creates the same barrier before enqueueing `start_aks`.
  A final failed/revoked lifecycle task records an explicit `aks_<action>_failed` admission
  reason and stays fail-closed until an operator retries the lifecycle action. Conflicts with
  an already-running scale/start/stop ARM operation release the worker slot and use bounded
  delayed Celery retries instead of blocking or failing immediately.
- `api/services/auto_warmup_reconcile.py` records each warmup Job ID before enqueueing the
  side effect, allowing admission to verify the exact post-lifecycle warmup generation. A
  superseded correlation or broker enqueue failure never launches an untracked warmup; it
  releases the in-flight lease and marks the seeded row failed so the next reconcile can retry
  and replace the token-scoped record.
- `api/tasks/servicebus/tasks.py` and `api/services/blast/resident_consumer.py` use the same
  pre-receive and pre-submit admission path.
- The existing `SERVICEBUS_ATOMIC_CLAIM` and `SERVICEBUS_DRAIN_SINGLEFLIGHT` guards now default
  ON after their June soak/load validation. Code also falls back to serial drain if atomic claim
  is explicitly disabled while concurrency is greater than 1.

No new dependency, role assignment, public network path, browser SAS token, or Azure resource
is introduced. The existing `dashboardsingletons` table stores the additional small state rows.

## Validation evidence

- Focused lifecycle/admission suite — 364 passed.
- `uv run pytest -q api/tests` — 4,801 passed, 4 skipped.
- `uv run ruff check api` — clean.
- `uv run mypy --strict --follow-imports=skip api/services/aks/execution_admission.py
  api/services/aks/execution_admission_state.py api/services/state/singletons.py` — clean.
- `uv run python scripts/docs/check_frontmatter.py` and
  `DISABLE_MKDOCS_2_WARNING=true uv run mkdocs build --strict` — passed.
- New regression coverage includes barrier-before-enqueue ordering, enqueue cancellation,
  queue-autostart ordering, fail-closed state reads, final lifecycle failure, exact target-node
  readiness, correlated warmup pending/running/failed/completed states, resident-consumer
  deferral, the receive-to-submit race, no stale allow caching, and lifecycle-interrupted
  external jobs/bridges.

Live Azure lifecycle execution is intentionally not triggered by this change-note validation;
node scaling and stop/start are cost-bearing, and the deterministic mocked tests exercise the
state-machine boundaries without mutating the shared cluster.
