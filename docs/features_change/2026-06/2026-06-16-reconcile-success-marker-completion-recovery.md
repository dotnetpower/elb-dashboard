---
title: Reconcile recovers completed jobs from the durable SUCCESS marker
description: Stop the stale-job reconciler from marking a finished BLAST job worker_lost when its cluster was torn down after upload; trust the durable metadata/SUCCESS.txt Storage marker as completion ground truth.
tags:
  - blast
  - operate
---

# Reconcile recovers completed jobs from the durable SUCCESS marker

## Motivation

A user reported an inconsistency: the **status API** returns `complete`, but
downloading the result XML via the **results API** fails, and searching the
same `job_id` in **Recent searches** shows it as `failed`.

Investigation (App Insights / Log Analytics on `appi-elb-dashboard`,
`rg-elb-dashboard`) showed the known instances of this class were already fixed
across several commits:

* External-origin rows are no longer falsely marked `worker_lost` when the
  sibling is transiently unreachable (`572cd9d`, 2026-06-14 — the live cases
  `e1f0d24fdc74` / `2e0c684cdd95` predate it; **zero** `worker_lost` events
  recurred after the fix, 39 `completed` since).
* The stale `worker_lost` error badge on a completed row is suppressed at the
  display chokepoint (`job_state.py`).
* The sibling `completed`→`/results` 404 visibility race is gated by
  `RESULTS_VISIBILITY_GRACE_SECONDS` (elb-openapi `4.20`+, deployed `4.24`).
* The `core_nt` memory-fit submit failure (`981f74c3d130`) is fixed via the
  sharding-default promotion.

This change closes the **one remaining latent gap** in the same symptom class:
the stale-job reconciler's *time-based* `worker_lost` path (step 3, reached only
when the Celery result has expired **and** the K8s API **and** the sibling
OpenAPI plane are all unreachable) declared a quiet row `failed`/`worker_lost`
**without consulting the durable Storage ground truth**. A dashboard-submitted
job that actually finished — its results uploaded to Storage — but whose AKS
cluster was stopped/deleted right after (aggressive auto-stop) hits exactly that
conjunction, so the row was falsely marked `failed` even though its results are
sitting in Storage. That produces the reported divergence: status/download work
off the durable results, but Recent searches (the durable jobstate row) shows
`failed`.

## User-facing change

No UI change. A finished BLAST job whose cluster was torn down after it uploaded
its results is now reconciled to `completed` (and its stale `error_code` is
cleared) instead of being shown as `failed` in Recent searches. The three views
(status API, results download, Recent searches) converge on the durable Storage
truth.

## API / IaC diff summary

### Backend (`api/`)

* `api/services/blast/result_analytics.py` — new `has_blast_success_marker(
  storage_account, job_id)`: best-effort check for the durable
  `.../metadata/SUCCESS.txt` marker the cluster-side finalizer writes **last**
  (only after every result artifact is uploaded). Returns `False` on any error
  so a Storage hiccup never falsely completes a job. Mirrors the existing
  `runtime_failure.read_blast_runtime_failure` listing pattern.
* `api/tasks/blast/submit_runtime.py` — thin task-layer wrapper
  `_has_blast_success_marker(...)` (+ `__all__`), re-exported from
  `api/tasks/blast/__init__.py` alongside `_has_parseable_result_artifact`.
* `api/tasks/blast/reconcile_task.py` — `reconcile_stale_jobs` now checks
  `_has_blast_success_marker` **before** the time-based `worker_lost` write.
  When the marker is present the row is finalized via
  `_update_state(..., "completed", status="completed",
  event="reconcile_results_recovered", error_code="")` (sweeps orphan running
  steps, clears the stale error_code column, enqueues the artifact finalizer,
  emits the `blast`/`completed` feature event) and counted under `completed`.
  Marker absent → unchanged `worker_lost` behaviour.

### Why the SUCCESS marker (not just "result blobs exist")

`list_parseable_result_blobs` returns partial shard outputs when no merged blob
exists, so "an artifact exists" alone could falsely complete a partially-failed
sharded job. `metadata/SUCCESS.txt` is written only on success, last, and job
ids are unique uuid4s (no stale-marker reuse), so a present marker is
authoritative. This is the safe ground truth for a path that has **no** upstream
completion signal.

## Validation evidence

* New `api/tests/test_blast_success_marker.py` (4): marker present → True;
  absent (`FAILURE.txt` only) → False; empty inputs → False; Storage error →
  False (fail-safe).
* New `api/tests/test_blast_tasks.py::test_reconcile_recovers_completed_quiet_row_from_success_marker`:
  a quiet, k8s-unreachable row with the marker is reconciled to `completed`
  (`error_code` cleared, `reconcile_results_recovered` history event), not
  `worker_lost`. Existing `test_reconcile_marks_old_quiet_row_worker_lost`
  still green (no marker / empty storage account → `worker_lost`), proving the
  legacy path is unchanged.
* `uv run pytest -q api/tests/test_blast_success_marker.py api/tests/test_blast_tasks.py api/tests/test_local_to_blast_job.py api/tests/test_job_artifacts.py` → 208 passed.
* `uv run ruff check` on all touched files → clean.

## Remaining notes

* The sub-case where results genuinely do **not** exist (the sibling reports
  `completed` after the SUCCESS-marker grace but no files materialized) is a
  sibling-side concern (`elastic-blast-azure` is read-only here); the dashboard
  correctly shows `failed` and the download correctly fails — the honest,
  consistent outcome.
