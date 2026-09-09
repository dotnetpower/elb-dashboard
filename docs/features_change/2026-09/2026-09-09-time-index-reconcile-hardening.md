---
title: Time-index reconciliation hardening
description: Stop rewriting healthy job index rows, bound periodic repairs, and expose reconciliation failures truthfully.
tags: [operate, blast]
---

# Time-index reconciliation hardening

## Motivation

The hourly [Celery](https://docs.celeryq.dev/en/stable/) reconciliation pass
scanned every non-deleted job and unconditionally rewrote both secondary-index
rows. A production pass on 2026-09-09 processed 20,417 jobs in about 7 minutes
26 seconds, which meant approximately 40,834 Azure Table writes even when the
index was already healthy. The task also had no schedule expiry or cross-revision
single-flight guard, and converted failures into successful Celery results.

## Change

- Existing index entities are read but no longer rewritten; only missing owner
  and global index rows are created.
- `written` now reports actual index entities created, including both rows per
  previously unindexed job.
- A token-owned [Redis](https://redis.io/docs/latest/) lock prevents old and new
  Container App revisions from reconciling concurrently.
- Explicit soft and hard limits remain below the hourly schedule, and delayed
  beat messages expire before a later hourly tick can queue behind them.
- Soft timeouts and unexpected failures propagate so Celery records `FAILURE`
  instead of a false success.

The jobs API, index keys, ordering, pagination, and feature gate are unchanged.

## Validation

- `uv run pytest -q api/tests/test_jobstate_time_index.py` - 34 passed.
- Tests cover first repair, no-write second repair, lock contention, lock token
  release, ordinary failure propagation, soft-timeout propagation, and schedule
  deadline ordering.
