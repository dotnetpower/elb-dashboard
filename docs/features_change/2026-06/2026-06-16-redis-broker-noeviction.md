---
title: Stop the in-revision Redis broker from evicting queued jobs
description: Switch the Container App Redis sidecar from allkeys-lru to noeviction so the Celery broker never drops enqueued BLAST/ACR/AKS tasks under memory pressure, fixing the "queuing doesn't work" symptom while keeping the maxmemory guardrail.
tags:
  - infra
  - operate
  - blast
---

# Stop the in-revision Redis broker from evicting queued jobs

## Motivation
The single in-revision Redis sidecar is simultaneously the Celery **broker**
(db0), **result backend** (db1), and **ops/durable cache** (db2). It was started
with `--maxmemory-policy allkeys-lru` (added in the 2026-05-23 performance
hardening batch to bound memory). For a cache that policy is fine; for a
**broker** it is a documented anti-pattern. Under memory pressure Redis evicts
*any* key it chooses — including the broker's queue lists, the unacked-task
hashes, and the durable OpenAPI runtime-config keys. The visible symptom is that
operator-triggered work (BLAST submit, ACR build, AKS provision) is accepted by
the API, never runs, and never reaches a terminal state — i.e. "queuing doesn't
work well".

## User-facing change
- Enqueued long-running jobs are no longer silently dropped by the broker. Once
  a task is published it stays in the queue until a worker consumes it.
- No API, UI, or schema change. Behaviour change is limited to broker
  durability under memory pressure.

## API / IaC diff summary
- `infra/modules/containerAppControl.bicep`: Redis sidecar
  `--maxmemory-policy allkeys-lru` → `noeviction`. The `--maxmemory 384mb` cap is
  kept intentionally: with `noeviction`, a memory-pressure write fails loudly
  (and surfaces on the sidecar metrics card) instead of either dropping queued
  work or growing until the replica is OOM-killed. Memory stays bounded in
  practice because the result backend honours `CELERY_RESULT_EXPIRES` (≤ 2 h) and
  the ops caches set per-key TTLs.
- `api/tests/test_redis_broker_eviction_policy.py`: new regression guard that
  fails if the Redis sidecar ever uses an `allkeys-*` / `volatile-*` policy, or
  drops the `--maxmemory` cap.

## Validation evidence
- `uv run pytest -q api/tests/test_redis_broker_eviction_policy.py api/tests/test_dockerfile_single_worker.py` — **4 passed**.
- `uv run ruff check api/tests/test_redis_broker_eviction_policy.py` — **clean**.
- `az bicep build --file infra/main.bicep --stdout` — **builds** (only the
  standard Bicep version-upgrade warning).
