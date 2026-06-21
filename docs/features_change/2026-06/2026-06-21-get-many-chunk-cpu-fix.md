---
title: Fix api sidecar 100% CPU from non-idempotent external-jobs sync
description: Chunk JobStateRepository.get_many OData lookups so large id batches no longer fail and re-create every row on each poll.
tags:
  - operate
  - blast
---

# Fix api sidecar 100% CPU from non-idempotent external-jobs sync

## Motivation

The `api` sidecar pegged at 100% CPU (0.5 vCPU) in a deployed environment. Console
logs showed `api.services.blast.external_jobs` emitting
`external job sync: created=1029 updated=0` on **every** `/api/blast/jobs` poll,
i.e. the sync re-created all 1029 external OpenAPI jobs each time instead of
recognising them as already-persisted.

### Root cause

`_sync_external_jobs_to_table` looks up the existing rows in one batch via
`JobStateRepository.get_many(job_ids)`. `get_many` folded **all** ids into a
single OData `$filter` (`(PartitionKey eq '<id>' and RowKey eq 'current') or …`).
With 1029 ids the filter is ~60 KB, exceeding the Azure Table Storage request-URI
length, so the query failed with HTTP 400. The caller wraps `get_many` in a
best-effort `try/except` and falls back to `existing_map = {}`, so every job
looked new. Each poll then:

1. issued one over-length (failing) `get_many` query, then
2. called `repo.create()` 1029 times — each hitting `ResourceExistsError`
   (the row already exists) which triggers a second point-read round-trip.

That is ~2000+ Table operations per poll, multiplied by the dashboard's
multi-times-per-second `/api/blast/jobs` polling → CPU saturation. The work was
also pure waste (no state actually changed).

## User-facing change

None functionally. The dashboard behaves identically, but the `api` sidecar CPU
drops back to idle levels and the `external job sync: created=…` storm stops once
the existing rows are recognised (`updated=0`, `created=0` for an unchanged set).

## Code change summary

* [api/services/state/repository.py](../../../api/services/state/repository.py)
  * `get_many` now chunks the lookup into batches of `_GET_MANY_FILTER_CHUNK`
    (50) ids, so the OData `$filter` stays small regardless of batch size. The
    result map is merged across chunks. Behaviour is unchanged for the existing
    `list_for_owner` caller (≤ 500 ids) and the external-jobs sync (1000+ ids)
    now resolves correctly.
  * Docstring/comment updated; the stale "500 ids stay well under the limit"
    note removed.

## Validation

* `uv run pytest -q api/tests/test_state_repo.py -k get_many` — 2 passed
  (existing single-query test + new `test_get_many_chunks_large_id_set` that
  passes 120 ids, asserts 3 chunked queries each < 8 KB, and all rows found).
* `uv run pytest -q api/tests/test_state_repo.py api/tests/test_external_blast_api.py api/tests/test_blast_tasks.py` — 274 passed.
* `uv run pytest -q api/tests` — 4134 passed, 3 skipped.
* `uv run ruff check api/services/state/repository.py api/tests/test_state_repo.py` — clean.

### Live deploy verification

After patching the `api` (+ `worker`/`beat`) sidecar image on the affected
environment, the live `api` console logs confirm the storm is gone:

* `external job sync: created=1029 updated=0` (every poll, pre-fix) →
  `external job sync: created=0 updated=…` once (a one-time heal of stale
  rows), then no further sync log lines at all — i.e. steady-state
  `created=0 updated=0`, which the `if created or updated:` guard does not log.
* Zero `created=1029` lines on the new revision.
* `/api/blast/jobs` latency back to single-digit milliseconds (4–6 ms).
