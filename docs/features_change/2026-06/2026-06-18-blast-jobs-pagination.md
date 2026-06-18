---
title: Recent searches page-size cap and BLAST jobs pagination envelope
description: Recent searches now requests only the most-recent 20 jobs and /api/blast/jobs returns an OpenAPI-standard page envelope with an honest has_more flag.
tags:
  - blast
  - user-guide
---

# Recent searches page-size cap and BLAST jobs pagination envelope

## Motivation

The Recent searches initial load felt slow. The frontend never sent a page-size
to `/api/blast/jobs`, so it always pulled the backend default (50) and rendered
the whole list, while the underlying Azure Table scan reads the genuinely
most-recent rows from a non-ordered table. There was also no pagination concept
on the list contract, so a client had no honest signal that more jobs existed
beyond the page it received.

## User-facing change

* **Recent searches** now requests only the most-recent **20** jobs on load
  (`RECENT_SEARCHES_PAGE_SIZE`). The backend still returns the genuinely
  most-recent N, so nothing newer is hidden.
* **`GET /api/blast/jobs`** now returns an additive, OpenAPI-standard `page`
  envelope alongside the existing top-level `jobs` array:

  ```json
  {
    "jobs": [ ... ],
    "page": { "limit": 20, "returned": 20, "has_more": true },
    "meta": { ... }
  }
  ```

  `has_more` is honest: the route over-fetches by one row (`limit + 1`) across
  every source, so a full page reliably signals "there is at least one more".
  The probe row is dropped by the final slice and never reaches the client.

## API / IaC diff summary

* `api/services/response_contracts.py` — new `build_page(limit, returned,
  has_more, next_cursor=None)` helper. `next_cursor` is reserved for true
  cursor pagination and omitted while None.
* `api/routes/blast/jobs.py` — `_compute_blast_jobs_response` fetches
  `limit + 1` from `list_for_owner` / `list_for_scope` / `list_all` and from
  the external detail-enrich budget, then returns `jobs[:limit]` plus the
  `page` envelope. Top-level `jobs` / `count` / `meta` / degraded fields are
  unchanged (additive, backward compatible).
* `web/src/api/blast.ts` + `blast.types.ts` — `listJobs` accepts an optional
  `limit`; new `ApiPage` / `BlastJobsListResponse` types.
* `web/src/hooks/useScopedBlastJobs.ts` — optional `limit` option, threaded into
  the request and the query key.
* `web/src/pages/BlastJobs/useBlastJobsState.ts` — Recent searches passes
  `limit: 20`.
* No IaC change.

## Known follow-ups (not in this change)

* **Time-ordered secondary index** for `jobstate`. The genuinely-most-recent
  ordering still reads the filtered set and sorts in process
  (`_list_recent_sorted`, bounded by `JOBSTATE_LIST_SCAN_CAP`). A time-ordered
  index would turn "most recent N" into a bounded page read and unlock a real
  `next_cursor`. The `page` envelope shape is already forward-compatible.
* **External `/v1/jobs`** pagination lives in the sibling
  `dotnetpower/elastic-blast-azure` service and is out of scope here; this
  repo's proxy is ready to pass a cursor through once the index lands.

## Validation evidence

* `uv run pytest -q api/tests` — 3942 passed, 3 skipped.
* New tests: `test_jobs_list_page_envelope_has_more_when_more_rows_exist`,
  `test_jobs_list_page_envelope_has_more_false_on_last_page` in
  `api/tests/test_blast_jobs_routes.py` (assert slice, `returned`, `has_more`,
  and the `limit + 1` probe).
* `uv run ruff check api` — clean on touched files.
* `cd web && npm run build` — type-checks and builds clean.
* `cd web && npm test -- --run` — 900 passed.
