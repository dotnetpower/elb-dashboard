# Service Bus jobs keep their "servicebus" source label in Jobs / Recent searches

**Date:** 2026-06-17
**Area:** External job projection (`api/services/blast/external_jobs.py`, `api/services/blast/job_state.py`)

## Motivation

Verifying that a Service Bus queue request is reflected accurately on the
dashboard surfaced two findings:

1. **Status is accurate** — a queue-drained job appears in Jobs and Recent
   searches and progresses `queued → running → completed` correctly.
2. **Origin was mislabeled** — the same job showed `submission_source: external_api`
   instead of `servicebus`. Over the wire a queue-drained job is submitted to the
   sibling as `external_api` (the sibling's enum has no `servicebus` value), so the
   `/v1/jobs` row always reports `external_api`, and the list view (which renders
   the fresh sibling row) downgraded the origin. The Message Flow card already
   showed `servicebus`, so the two views disagreed.

## User-facing change

Jobs and Recent searches now label a queue-originated job `servicebus`, matching
the Message Flow card. The coarse `source` flag is unchanged (`external_api` for
external-origin rows); the precise `submission_source` field now carries the true
origin.

## Fix summary

- `api/services/blast/external_jobs.py`
  - New `_stored_submission_source(state)` reads the dashboard-recorded origin
    from the stored row (`payload.external.submission_source`, then the payload
    top level).
  - `_sync_external_jobs_to_table` recovers that marker onto the fresh sibling
    row before projecting, so a job the dashboard knows is `servicebus` is never
    downgraded to the sibling's `external_api`. The recovery only honours a
    marker the dashboard itself set (the stored row preserves it because the
    update path never rewrites the payload) — it never invents `servicebus` for a
    genuine `external_api` job.
- `api/services/blast/job_state.py`
  - `_local_to_blast_job` now surfaces a top-level `submission_source` via the new
    `_resolve_local_submission_source` helper (nested `external.submission_source`
    → payload top level → `external_api`/`dashboard` fallback), so the send-time
    `servicebus` placeholder and the shared drained row are labelled correctly on
    the local-row path too.

## Known limitation (logs) — cross-repo, not fixed here

Live **execution step timeline** (preparing / configuring / submitting / running)
IS visible for queue/external jobs via the SSE snapshot. **Raw Kubernetes pod log
lines are NOT streamed** for external/Service Bus jobs because the dashboard
follows pods by the elastic-blast `elb-job-id` label, which the sibling's
`/v1/jobs` API does not expose (it only returns the OpenAPI `job_id`). This
affects all `external_api` submits equally, not just Service Bus. A sibling
(`dotnetpower/elastic-blast-azure`) enhancement to expose the `elb-job-id` would
let the dashboard discover and follow the pods; that is tracked separately.

## Validation

- `uv run pytest -q -n 0 api/tests/test_local_to_blast_job.py` (incl. 3 new
  `submission_source` tests) and the two new
  `api/tests/test_external_blast_api.py::test_sync_external_*_servicebus_*` tests
  — all pass; full `test_external_blast_api.py` 104 passed.
- `uv run ruff check` on the four touched files — clean.
- Live: a Service Bus request (`POST /api/settings/service-bus/send`, `core_nt`)
  appeared in Jobs / Recent searches and progressed `queued → running →
  completed`; the execution step snapshot streamed over SSE.
