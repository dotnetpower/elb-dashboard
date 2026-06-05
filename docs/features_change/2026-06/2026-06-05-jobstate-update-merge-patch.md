---
title: jobstate update() submits a MERGE patch, not the full snapshot
description: Fix a cross-field lost-update race where a stale read-back overwrote concurrently-changed jobstate fields.
tags:
  - blast
  - architecture
---

# jobstate `update()` submits a MERGE patch, not the full snapshot

## Motivation

`JobStateRepository.update()` read the full jobstate row
(`dict(get_entity(...))`), mutated a few fields in that snapshot, and wrote the
**entire snapshot** back with `UpdateMode.MERGE`.

`UpdateMode.MERGE` overwrites every property present in the submitted entity. By
resubmitting the full read-back snapshot, the call reverted **any field a
concurrent writer had changed since our read** — even fields this call never
intended to touch. The api and worker run as separate Container Apps sidecars
hitting the same `jobstate` table, so this cross-process race is real:

* The submit route enqueues the Celery task and then calls
  `repo.update(job_id, task_id=task_id)`.
* The worker task has already started and called
  `repo.update(job_id, status="running", phase=...)`.
* With the old full-snapshot MERGE, the submit route's write carried its stale
  `status="queued"` and **clobbered the worker's fresh `status="running"`
  back to `queued`**, so the UI showed the job flipping backwards.

## User-facing change

* Concurrent updates that touch **different** fields no longer clobber each
  other; a job no longer visibly regresses (e.g. `running` → `queued`) when the
  submit route's `task_id` write races the worker's status write.
* No API shape change; `update()` signature, return type, and history append are
  unchanged. Same-field concurrent writes remain last-writer-wins (unchanged
  semantics).

## Implementation

`update()` now builds a minimal MERGE **patch** containing
`PartitionKey` / `RowKey` plus only the fields the call actually set
(`status`, `phase`, `task_id`, `error_code`, `payload_json` + canonical
metadata, and the always-bumped `updated_at`), and submits that patch with
`UpdateMode.MERGE`. The full read snapshot `e` is still mutated locally so the
returned `JobState` reflects this call's changes, but it is no longer written
back wholesale.

Scope note: this fixes cross-field lost-updates. The same-field race (two
writers both setting `status`) stays last-writer-wins, which is the pre-existing
semantics; a domain rule preventing a `deleted` tombstone from being overwritten
by a late progress write is a separate concern and intentionally out of scope
here.

## API / IaC diff summary

* `api/services/state/repository.py` — `update()` submits a delta patch instead
  of the full snapshot. No infra change.

## Validation evidence

* New regression test `test_update_submits_only_the_changed_fields` asserts the
  submitted entity for `update(job_id, task_id=...)` carries only the routing
  keys + `task_id` + `updated_at`, and NOT `status` / `phase` / `payload_json`
  from the stale snapshot.
* `uv run ruff check api/services/state/repository.py api/tests/test_state_repo.py` — clean.
* `uv run pytest -q api/tests/test_state_repo.py` — 18 passed.
* Consumer suites (`test_celery_failure_visibility.py test_blast_results_routes.py
  test_route_contracts.py test_auto_warmup.py test_aks_recent_failed_provisions.py`)
  — 65 passed.
* Full backend sweep: `uv run pytest -q api/tests` — 2775 passed, 3 skipped.
