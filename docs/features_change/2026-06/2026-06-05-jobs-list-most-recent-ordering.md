---
title: BLAST jobs listing returns the genuinely most-recent N
description: Fix jobstate list queries so the most-recent jobs are never dropped from the listing.
tags:
  - blast
  - architecture
---

# BLAST jobs listing returns the genuinely most-recent N

## Motivation

`JobStateRepository.list_for_owner` / `list_all` / `list_for_scope` powered the
`/api/blast/jobs` listing by issuing `query_entities(filter, results_per_page=limit)`
and breaking out of the iterator once `limit` rows were collected, then sorting that
collection by `created_at` descending.

Azure Table Storage has **no server-side ordering**, and jobstate rows use a random
`uuid4` as their `PartitionKey` (`RowKey` is the constant `"current"`). The first
`results_per_page=limit` page therefore returns an **arbitrary** subset ordered by
random uuid. Sorting only that page silently dropped the genuinely newest jobs once an
owner had **more than `limit` jobs** (default `limit=50`, max `500`): a freshly
submitted job whose uuid sorted late could be absent from the page entirely and never
appear at the top of the list.

This is a correctness/UX defect surfaced during a performance critique of the state
repository.

## User-facing change

* The Recent searches / Jobs list now always shows the true most-recent `limit` jobs,
  regardless of how many jobs the owner (or scope) has accumulated.
* No API shape change — the route, response contract, and `jobs_list_cache` (10 s TTL)
  are unchanged.

## Implementation

* New module helper `JobStateRepository._list_recent_sorted(filter_expr, *, limit,
  include_payload)` reads the **full** filtered set, sorts by `created_at` descending,
  and truncates to `limit`.
* The scan is bounded by `_list_scan_hard_cap()` (default `5000`, override
  `JOBSTATE_LIST_SCAN_CAP`) so a pathological table cannot OOM the worker; `$top` is
  still clamped to the Azure 1000-row page ceiling via `_clamp_page_size` so the SDK
  paginates. Hitting the cap logs a `WARNING` flagging that a time-ordered secondary
  index is the proper long-term fix.
* `list_for_owner`, `list_all`, and `list_for_scope` now delegate to the helper.
  Filter strings, method signatures, defaults, and return types are unchanged.

Cost note: under the existing 10 s `jobs_list_cache` the underlying scan runs at most
once per cache window per (caller, limit, scope), and `include_payload=False`
(the list path) keeps each row to the summary projection — so the correctness fix is
effectively perf-neutral for realistic job counts while bounded for pathological ones.

## API / IaC diff summary

* `api/services/state/repository.py` — add `_list_scan_hard_cap` + `_list_recent_sorted`;
  refactor `list_for_owner` / `list_all` / `list_for_scope` to use them.
* No infra change.

## Validation evidence

* New regression test `test_list_for_owner_returns_newest_beyond_first_page` places the
  newest rows LAST in iteration order and asserts they are returned for `limit=3`
  (a first-page read would have missed them).
* `uv run ruff check api/services/state/repository.py api/tests/test_state_repo.py` — clean.
* `uv run pytest -q api/tests/test_state_repo.py` — 17 passed.
* Consumer suites: `test_blast_results_routes.py test_route_contracts.py
  test_auto_stop_evaluator.py test_blast_job_state_scope.py test_local_to_blast_job.py
  test_jobs_list_cache.py test_openapi_token.py` — 119 passed.
* Full backend sweep: `uv run pytest -q api/tests` — 2774 passed, 3 skipped.
