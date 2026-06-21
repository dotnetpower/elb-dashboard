# Surface the Service Bus request correlation id + true origin on the Jobs list

**Date:** 2026-06-21

## Motivation

During a live Service Bus load + BLAST end-to-end validation, queue-drained jobs
appeared in the **Jobs / Recent searches** list labelled `dashboard` with an
empty correlation id, even though the job detail view and the OpenAPI view both
knew the job came from the Service Bus queue and carried its
`external_correlation_id`. That broke the operator's ability to trace a Service
Bus request message (its `external_correlation_id`) to the job it produced from
the Jobs list — the primary "is the queue request showing properly?" signal.

### Root cause

A queue-drained job's `submission_source` (`"servicebus"`) and
`external_correlation_id` were stored **only inside** `payload.external.*`. The
Jobs list reads JobState **columns only** (`include_payload=False`) for speed, so
with no payload loaded the projection fell back to `submission_source="dashboard"`
and emitted no correlation id. The detail view loaded the payload and recovered
`submission_source`, but `_local_to_blast_job` never emitted
`external_correlation_id` at all.

## User-facing change

- The Jobs list and the job detail now show the **true origin** of a
  queue-drained job (`servicebus` → "queue") instead of mislabelling it
  `dashboard`.
- Both surfaces now expose `external_correlation_id`, so an operator can match a
  Jobs row to its originating Service Bus request message.
- Dashboard-native jobs are unchanged (`source=dashboard`, no correlation id).

## API / code change summary

- `api/services/state/job_state.py`
  - `JobState` gains durable `external_correlation_id` + `submission_source`
    columns. `to_entity` backfills them from the explicit field **or** the
    payload (`payload.external.*` / payload top level), so a row built with only
    a payload still persists the columns. `from_entity` reads them back.
  - Added `_resolve_external_correlation_id` / `_resolve_payload_submission_source`
    helpers and the two new columns to `_JOBSTATE_SUMMARY_SELECT` so the
    column-only list read (`include_payload=False`) fetches them.
- `api/services/blast/job_state.py`
  - `_resolve_local_submission_source` accepts the durable column (prefers it
    over the payload, so the list surfaces the queue origin).
  - `_local_to_blast_job` emits `external_correlation_id` (column → payload
    fallback) and keeps `source` consistent with `submission_source`.
- `web/src/api/blast.types.ts` — `BlastJobSummary` gains optional
  `submission_source`, `source`, `external_correlation_id`.
- `web/src/pages/BlastJobs/jobSource.ts` — `jobSubmissionSource` prefers the
  top-level `submission_source` field (list rows omit the payload).

No IaC change. The new Table columns are additive and schemaless; legacy rows
without them resolve from the payload on the detail view and read empty on the
list (forward-looking — new drained rows carry the columns).

## Validation

- `uv run pytest -q api/tests` — 4145 passed, 3 skipped.
- New backend tests: `test_state_repo.py::test_job_state_round_trips_servicebus_correlation_columns`,
  `test_local_to_blast_job.py` (+3: column origin, payload fallback, native row).
- Frontend: `web/src/pages/BlastJobs/jobSource.test.ts` (+1 top-level column
  case) — 6 passed; `npm run build` clean.
- Live (moonchoi `elb-cluster-01`): drained Service Bus jobs confirmed the gap
  (detail `submission_source=servicebus` but `external_correlation_id=None`;
  list `source=dashboard`) before the fix; re-verified after deploy.
