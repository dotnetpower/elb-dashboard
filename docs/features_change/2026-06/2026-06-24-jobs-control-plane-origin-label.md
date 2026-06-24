---
title: BLAST Jobs User column — distinguish control-plane sends ("queue (dashboard)", "api (dashboard)")
description: The Jobs/Recent-searches User column now shows whether a queue job was enqueued by the dashboard control plane ("queue (dashboard)") or by an external producer straight to the namespace ("queue"), using a spoof-resistant send-time placeholder signal. API submits are labelled "api (dashboard)".
tags:
  - blast
  - ui
---

# Jobs User column: control-plane origin labels (2026-06-24)

## Motivation

A Service Bus queue job could be enqueued two ways and the User column showed
"queue" for both: (1) by the dashboard's own send route (control plane), or
(2) by an external service that connects straight to the namespace. Operators
asked to tell them apart — and likewise to mark API-facade submits.

## User-facing change

The **User column** (and the value it derives from) now distinguishes origin:

| Origin | Label |
|--------|-------|
| Dashboard send route → Service Bus | **`queue (dashboard)`** |
| External producer → Service Bus directly | `queue` |
| External service → dashboard API facade (`POST /api/v1/elastic-blast/submit`) | **`api (dashboard)`** |
| Dashboard UI New Search | `<user>` (upn local part) |

The source **filter tabs** (All sources / UI / API / Queue) are unchanged — they
still group by the coarse source (`ui` / `api` / `servicebus`); only the
displayed label gained the `(dashboard)` distinction.

## How the control-plane signal works (spoof-resistant)

The dashboard send route (`POST /api/settings/service-bus/send`) writes a
**send-time placeholder** jobstate row keyed by the correlation id BEFORE the
message reaches the queue. An external producer that enqueues straight to the
namespace cannot write to the dashboard's jobstate table, so the **presence of a
placeholder** is a trustworthy signal that the request came through the control
plane — it cannot be forged by setting a message property.

* The drain (`_persist_drain_row_and_trace`) checks `placeholder_exists(corr)`
  (before superseding it) and stamps `queue_origin = "control_plane" | "external"`
  on the durable row's `payload.external`.
* `queue_origin` is recovered onto the projected summary the same way
  `submission_source` is (`_stored_queue_origin` mirrors `_stored_submission_source`),
  and surfaced for local rows via `_resolve_local_queue_origin` (a placeholder
  row reads `control_plane` immediately, before the drain).
* API submits always come through the control-plane API facade, so the frontend
  labels `api` as `api (dashboard)` unconditionally.

## API / IaC diff summary

* Backend (additive field `queue_origin` on the job summary):
  `api/services/blast/servicebus_placeholder.py` (`placeholder_exists`),
  `api/tasks/servicebus/tasks.py` (stamp on drain),
  `api/services/blast/external_jobs.py` (`_stored_queue_origin` + relabel),
  `api/services/blast/external_job_projection.py` (surface on projection),
  `api/services/blast/job_state.py` (`_resolve_local_queue_origin` + surface),
  `api/services/state/job_state.py` (durable `queue_origin` column derived from
  payload in `to_entity` so the payload-less list view surfaces it).
* Frontend: `web/src/api/blast.types.ts` (`queue_origin?`),
  `web/src/pages/BlastJobs/jobSource.ts` (`jobSourceLabel` takes queue origin),
  `web/src/pages/BlastJobs/JobRow.tsx` (User column).
* No IaC / Container App template change.

## Deployment + live verification (customer environment)

Deployed `api` + `frontend` to the customer Container App (revision `0000157`,
RunningAtMaxScale, both new images), through the dashboard managed identity (the
caller has no direct Service Bus RBAC):

* A control-plane send (`POST /api/settings/service-bus/send`) created a
  placeholder; `GET /api/blast/jobs` showed it with
  `submission_source=servicebus` + **`queue_origin='control_plane'`** while
  `queued`, and it **persisted as `control_plane` after the drain rejected it**
  (status `failed`) → renders **"queue (dashboard)"**.
* Pre-existing queue rows (drained before this deploy) carry `queue_origin=''`
  → render plain **"queue"**, confirming the distinction.
* The probe used a `db=/cp-label-probe` value the sibling rejects with 400, so
  **no real BLAST ran**; its single DLQ entry was purged afterwards (the
  customer's 3 pre-existing DLQ entries untouched). No Azure resource created,
  no shared config repointed (charter §13).

## Validation evidence

* `uv run pytest -q api/tests/test_servicebus_placeholder.py
  api/tests/test_local_to_blast_job.py api/tests/test_servicebus_tasks.py
  api/tests/test_external_blast_api.py api/tests/test_external_job_projection.py
  api/tests/test_blast_jobs_routes.py` — all green (queue_origin stamp,
  placeholder_exists, local + projected surfacing, no regressions).
* `uv run ruff check` — clean on touched files.
* `cd web && npx vitest run src/pages/BlastJobs/jobSource.test.ts` — 7 passed
  (control-plane vs external label cases).
* `cd web && npm run build` — green.
