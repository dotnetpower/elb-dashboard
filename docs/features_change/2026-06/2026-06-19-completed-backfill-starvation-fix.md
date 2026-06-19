---
title: Completed-job runtime-metric backfill no longer starves older jobs
description: Fix the random-PartitionKey fixed-window scan in list_completed so K8s runtime-metric backfill processes the genuinely most-recently-completed jobs instead of an arbitrary fixed subset.
tags:
  - operate
  - blast
---

# Completed-job runtime-metric backfill no longer starves older jobs

## Motivation

The `backfill_completed_runtime_metrics` Celery task (runs every ~300 s) reads
completed BLAST jobs via `JobStateRepository.list_completed` and fills in K8s
container runtime timestamps for any that are missing them. It silently stopped
making progress once a deployment accumulated more than `limit` (default 50)
completed jobs.

## Root cause

`list_completed` issued a single capped Table page
(`query_entities(..., results_per_page=limit)` + `break` at `len >= limit`).
jobstate rows use a **random-uuid PartitionKey** (`new_job_id()` →
`str(uuid.uuid4())`), and Azure Table Storage returns rows in
`(PartitionKey, RowKey)` order, so that page is an arbitrary but **fixed**
lexical subset. The backfill task skips rows that already carry metrics, so once
that fixed window was fully backfilled, every later tick re-scanned the same
rows and made zero progress — any completed job outside the window was **starved
forever**. Same bug class as the auto-stop `history_scan_truncated` regression:
a hardcoded page-cap that is hit during normal accumulation and yields a wrong,
effectively-permanent outcome.

## User-facing change

* Completed jobs ranked beyond the first ~50 by PartitionKey now get their K8s
  container runtime metrics backfilled. Their Run detail page shows the precise
  blast / results-export container durations instead of leaving them blank.

## API / IaC diff summary

* `api/services/state/repository.py::list_completed`
  * Scans the full filtered set as lightweight summaries (bounded by the same
    `_list_scan_hard_cap()` as `_list_recent_sorted`, page size clamped to the
    Azure 1000 ceiling), sorts by `updated_at` (completion recency) descending,
    then re-fetches the full payload only for the top `limit` rows.
  * `updated_at` — not `created_at` — is the correct key: a long-running BLAST
    job can be created hours before it completes, and only recently-completed
    jobs still have a live (non-garbage-collected) K8s Job to read timestamps
    from, so backfilling old jobs is a no-op anyway.
* No IaC change. Method signature and return type are unchanged
  (`list[JobState]` with full payload).

## Validation evidence

* New regression `test_list_completed_returns_newest_first_no_starvation`: rows
  whose PartitionKey order is the reverse of their completion order are returned
  newest-first by `updated_at` (not lexical-first by PartitionKey), with full
  payload re-fetched.
* `uv run pytest -q api/tests/test_state_repo.py api/tests/test_blast_tasks.py`
  → 167 passed; full `uv run pytest -q api/tests` → 4015 passed, 3 skipped.
* `uv run ruff check api/services/state/repository.py api/tests/test_state_repo.py`
  → clean.
