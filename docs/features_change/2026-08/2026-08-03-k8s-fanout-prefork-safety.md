---
title: K8s fan-out prefork safety
description: Recreate inherited Kubernetes fan-out executors after Celery prefork and preserve Service Bus health task deadlines.
tags: [blast, operate]
---

# K8s fan-out prefork safety

## Motivation

The resident Service Bus consumer creates the shared Kubernetes monitoring
executor in its Celery parent. A later memory-recycled prefork child inherited
the executor object, but not its worker threads. Once the parent executor had
reached its 16-thread ceiling, the child could submit no replacement worker and
waited until the 45-second Service Bus health soft limit. Broad best-effort
error handling then converted that deadline into a successful
`cluster_warming` snapshot.

Application Insights also showed many Azure Table dependencies with result code
504. Those were investigated separately. Nearly every 3–9 ms config/singleton/
bridge attempt was followed within the same SDK operation by a successful retry.
Twelve outbox create attempts were final failures; they occurred after OpenAPI
had accepted the execution, so the confirmed bridge retained an empty status
marker and the transition reconciler retried the queued ACK without resubmitting
the BLAST job. They were not the source of the 45-second task delay or job loss.

## Operational change

- The process-wide Kubernetes fan-out executor records its owner PID and is
  cleared by an `after_in_child` fork hook. A replacement Celery child always
  creates its own worker threads and never joins copied parent threads.
- Warmup pod-log fan-out no longer nests a pool-waiting helper inside the same
  executor, removing a second saturation deadlock mode.
- Celery `SoftTimeLimitExceeded` now propagates through warmup, execution
  admission, Service Bus health aggregation, and feature-event emission.
  Ordinary Kubernetes, Storage, and logging failures retain their previous
  degraded best-effort behavior.
- Replacement Celery children drop auto-warmup, Service Bus config, bridge,
  outbox, and singleton Table clients inherited from the resident-consumer
  parent. The child replaces copied locks without closing parent-owned
  transports.
- The child uses dedicated after-fork resets for the shared credential, ARM
  clients, Kubernetes sessions/credential material/circuit breaker, and
  JobState repositories. It no longer calls normal credential-rotation cleanup,
  which can acquire copied locks and close transports inherited from the
  resident parent.

The platform-returned 504 rate should be compared before and after deployment.
The unsafe inherited clients are fixed here, but the telemetry proves only that
the 504s were fast transient responses and not that every response shared this
root cause.

No API response field, Service Bus message contract, RBAC assignment, or
network policy changed.

## Validation

- A local pre-fix fork probe saturated all 16 executor threads, forked, and
  reproduced a child future timeout.
- `uv run pytest -q api/tests/test_k8s_warmup_status_parallel.py api/tests/test_execution_admission.py api/tests/test_service_bus_health.py api/tests/test_feature_events.py`
- `uv run ruff check api`
- `uv run pytest -q api/tests`
