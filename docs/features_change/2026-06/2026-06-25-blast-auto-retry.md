---
title: Auto-retry + quarantine for transient-failed BLAST jobs
description: A default-OFF beat sweep that auto-resubmits transient submit-phase BLAST failures with bounded backoff and quarantine, plus a failure-classification single source of truth surfaced on the job API.
tags:
  - operate
  - blast
---

# Auto-retry + quarantine for transient-failed BLAST jobs

## Motivation

Operations-readiness checklist section 4: "failed jobs auto-isolate + retry
(backoff), escalate only on repeated failure". The submit task already retries
**in-flight** (`max_retries=12`), but once a job reaches a terminal `failed`
state nothing resubmits it — a transient terminal-sidecar / Azure-auth / node
-warmup blip leaves a job dead that a later attempt would clear.

## User-facing change

* New **default-OFF** Celery beat sweep (`blast-auto-retry-failed-jobs`,
  180 s) that auto-resubmits **transient submit-phase** failures with
  exponential backoff and a per-job attempt counter, **quarantining** a job once
  its retry budget is exhausted or its submit kwargs cannot be reconstructed.
* The job API now carries `failure_classification` (category + `auto_retryable`)
  on every failed job and `auto_retry` (attempt counter / quarantine block) on
  the detail response, so the UI can show "auto-retrying (1/2)" or "quarantined".
* No behaviour change until `BLAST_AUTO_RETRY_ENABLED` is set (charter §12a
  Rule 4). The sweep returns immediately when disabled.

## Design — what is and is not auto-retried

`api/services/blast/failure_classification.py` is the single source of truth.
Only `FailureCategory.TRANSIENT_INFRA` (terminal/Azure/warmup submit-phase
failures that never reached the cluster) is `auto_retryable`. Deliberately **not**
auto-retried:

* `blast_search_failed` (K8s runtime) — resubmitting orphans the failed K8s job
  and re-stages the DB (cost); the user must inspect the error first.
* `worker_lost` / `cluster_stopped` / `cluster_not_found` — would queue forever
  against a stopped cluster.
* External-origin jobs (Service Bus drain / OpenAPI plane) — owned by the
  producing system, not the dashboard.
* Configuration/contract failures — deterministic, retrying changes nothing.

### Safety guardrails (critique + hardening)

* **Enqueue-before-flip**: the resubmit enqueues the `submit` task *before*
  flipping the row `failed → queued`, so a broker outage never strips a job out
  of its terminal state — the next sweep retries under backoff.
* **Double-submit guard**: only `status='failed'` rows are acted on, so a
  resubmitted job (now `queued`/`running`) is skipped next pass. Beat is
  single-instance.
* **Bounded everywhere**: `BLAST_AUTO_RETRY_MAX` (default 2) attempts, then
  quarantine; `BLAST_AUTO_RETRY_SWEEP_LIMIT` (default 5) resubmits per pass;
  `list_recent_failed` reads at most `BLAST_AUTO_RETRY_SCAN_LIMIT` (default 200)
  rows within a 24h `updated_at` window (no full failed-set payload scan);
  backoff caps at 1800 s.
* **Restore-or-quarantine**: `restore_submit_kwargs` returns `None` when any
  required submit field is missing → the job is quarantined rather than enqueued
  with a malformed submit.
* **Timeline hygiene**: the stale `_progress` block is dropped on resubmit so the
  fresh attempt rebuilds the step timeline; `jobhistory` keeps the prior audit
  trail.
* **Observability**: `auto_retry_scheduled` / `auto_retry_quarantined` history
  events + a `blast_auto_retry` App Insights custom event + a one-line sweep
  summary log.

## API / IaC diff summary

New backend modules: `api/services/blast/failure_classification.py`,
`api/services/blast/auto_retry.py`, `api/tasks/blast/auto_retry_task.py`. New
repo method `JobStateRepository.list_recent_failed` (time-windowed, capped). New
beat entry in `api/celery_app.py`. Job projection (`_local_to_blast_job`) gains
optional `failure_classification` + `auto_retry` fields. Gate registered in
[docs/operate/feature-gates.md](../../operate/feature-gates.md). No Bicep change,
no new Azure resource (the gate defaults OFF; env values are read at runtime).

## Validation evidence

* `uv run pytest -q api/tests/test_blast_failure_classification.py api/tests/test_blast_auto_retry.py api/tests/test_blast_auto_retry_task.py` — 48 passed.
* `uv run ruff check api` — all checks passed.
* `uv run pytest -q api/tests` — 4585 passed, 3 skipped, 1 failed. The single
  failure (`test_control_plane_env.py::test_bicep_references_every_guard_key`,
  re: `STORAGE_DATE_LAYOUT_ENABLED`) is pre-existing and unrelated — this change
  touches no `infra/` or `control-plane-env.json` file.
