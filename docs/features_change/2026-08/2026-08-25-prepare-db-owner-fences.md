---
title: Prepare database ownership fences
description: Stop superseded prepare workers before Kubernetes dispatch or destructive shard reconciliation.
tags:
  - blast
  - security
---

# Prepare database ownership fences

## Motivation

The prepare database state machine already used an ETag-protected `prepare_operation_id` owner token and checked it at task entry, after [AKS](https://learn.microsoft.com/azure/aks/what-is-aks) Job submission, during polling, and at terminal metadata commits. A cancellation or replacement operation could still take ownership while the old worker was building its Job manifest or immediately before the successful-copy path pruned and regenerated stable shard blobs.

## User-facing change

A superseded prepare operation now stops before submitting its AKS Job and before pruning or regenerating database shard artifacts. If ownership changes during Job submission, the existing deterministic cleanup path still removes the submitted Job and ConfigMap. The current owner remains authoritative and its metadata is never overwritten by the stale worker.

## API and runtime summary

- Added a no-op ETag compare-and-swap ownership fence immediately before AKS Job submission.
- Added the same ownership fence immediately before destructive consistency reconciliation in both the AKS fan-out and server-copy promotion paths.
- Kept the existing post-submit cleanup, terminal metadata CAS, operation IDs, routes, and response schemas unchanged.
- No Service Bus payload, authentication mode, Azure role, network policy, or infrastructure resource changed.

## Validation

- `uv run pytest -q api/tests/test_prepare_db_aks_task.py api/tests/test_prepare_db_hardening.py api/tests/test_prepare_db_routes.py`
- `uv run ruff check api/tasks/storage/prepare_db_via_aks.py api/routes/storage/prepare_db.py api/tests/test_prepare_db_aks_task.py`
