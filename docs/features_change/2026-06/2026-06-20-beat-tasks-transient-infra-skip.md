---
title: Beat tasks skip the tick on transient infra errors instead of crashing
description: Service Bus and prepare-db reconcile beat tasks now skip a tick on a transient Azure Storage/Service Bus DNS or connection blip instead of crashing with an UnpickleableExceptionWrapper, which had flooded App Insights with confusing exception rows for a self-healing condition.
tags:
  - operate
  - architecture
---

# Beat tasks skip the tick on transient infra errors instead of crashing

## Motivation

An App Insights / local-log error hunt surfaced a recurring, confusing exception
family on the worker role:

- `celery.utils.serialization.UnpickleableExceptionWrapper` wrapping
  `ServiceRequestError("... Failed to resolve '<storage>.table/blob.core.windows.net'
  ([Errno -3] Temporary failure in name resolution)")`.

Root cause: several **beat-scheduled** reconcile/integration tasks read Azure
Storage (Table/Blob) or Service Bus at the top of the task body. When a brief
platform DNS/network blip makes the endpoint briefly unresolvable, the azure-core
`ServiceRequestError` propagated out of the task. Celery's result backend cannot
pickle a raw `ServiceRequestError`, so it substituted an
`UnpickleableExceptionWrapper` — producing telemetry rows that look like a code
bug for what is actually a self-healing condition (the beat re-runs the task ~30 s
later and it succeeds).

Affected tasks:

- `api.tasks.servicebus.drain_and_resubmit`, `publish_transitions` (seen in the
  deployed App Insights, 9 rows from one ~2-minute blip)
- `api.tasks.servicebus.dlq_cleanup` (same vulnerability, preventatively guarded)
- `api.tasks.storage.reconcile_orphaned_prepare_db` (seen in local worker logs,
  40 rows — local dev cannot resolve the workload storage host)

## User-facing change

None visible in the UI. The dashboard already degraded gracefully; this removes
the misleading exception rows from telemetry and makes the beat tasks resilient
to transient connectivity blips.

## API / IaC diff summary

- New `api/tasks/transient.py`: `is_transient_infra_error()` classifier
  (`ServiceRequestError` / `ServiceResponseError` / builtin `ConnectionError`)
  and a `skip_tick_on_transient_infra` decorator that converts a transient error
  into a `{"skipped": "transient", "error_class": <name>}` result plus a one-line
  warning. Non-transient errors propagate unchanged so genuine bugs stay visible.
  The decorator forwards `*args`/`**kwargs`, so it works under `@shared_task` with
  `bind=True`.
- `api/tasks/servicebus/tasks.py`: stack the decorator under `@shared_task` on
  `drain_and_resubmit`, `publish_transitions`, `dlq_cleanup`.
- `api/tasks/storage/reconcile_orphan_prepare_db.py`: stack the decorator on
  `reconcile_orphaned_prepare_db`.
- No IaC change.

Scope notes — explicitly **not** changed (triaged as by-design or transient/external):

- K8s `requests.exceptions.SSLError` / `ConnectionError` against a stopping/
  restarting cluster API server: already absorbed by the per-cluster circuit
  breaker (`api/services/k8s/cluster_breaker.py`); the few rows per trip are the
  documented threshold leakage.
- `ClusterApiUnreachable`: the breaker working as intended (one row per trip).
- A single transient `GET /api/aks/openapi/token` 502 while the cluster was
  stopped: the proxy's correct bad-gateway response, not a bug.
- The remaining low-volume `ServiceRequestError` dependency rows from the same
  blip are genuine (if transient) dependency-failure signal and are left visible;
  only the downstream unpicklable task crash is removed.

## Validation evidence

- `api/tests/test_tasks_transient.py` (4): classifier, transient skip,
  non-transient propagation, `bind=True` arg/kwarg forwarding.
- `api/tests/test_servicebus_tasks.py` (+4): each beat task skips on
  `ServiceRequestError`/`ServiceResponseError`; a non-transient `ValueError`
  still propagates.
- `uv run ruff check api` clean; `uv run pytest -q api/tests` → 4112 passed,
  3 skipped.
- App Insights queried via the workload Log Analytics workspace
  (`log-elb-dashboard`, App* tables) since the App Insights resource is
  workspace-based and the classic `--app` query path returns empty.
