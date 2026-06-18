---
title: BLAST jobs list no longer blocks on cold-cache builds
description: Fast local-first first paint plus in-flight cache retention remove the multi-minute "JOBS loading…" spinner on the dashboard and BLAST Jobs page.
tags:
  - blast
  - ui
---

# 2026-06-18 — BLAST jobs list cold-cache latency fix

## Motivation

The dashboard cluster card's **JOBS** section (and the BLAST Jobs page) could
sit on a never-resolving "JOBS loading…" spinner. App Insights on the deployed
`ca-elb-dashboard` confirmed the `GET /api/blast/jobs` list call was bimodal:

| metric | value |
| --- | --- |
| p50 | 14 ms (cache hit) |
| p90 | ~247 s |
| p95 | ~731 s |
| max | ~1336 s (~22 min) |

A single cold-cache request fanned out to **~1,400 `TableClient.get_entity` +
~525 `create_entity` + ~900 `dashboardsingletons` reads** — the per-active-job
K8s status refresh plus the external OpenAPI `/v1/jobs` discovery + Table sync,
all serial. Two failure modes resulted:

1. **First paint blocked.** A cold key paid the full synchronous build, so the
   SPA's first poll (`jobsQuery.isLoading`) showed the spinner for minutes.
2. **Recurrence.** The stale-while-revalidate stale ceiling was 70 s, but the
   background rebuild took longer than 70 s, so the entry was dropped to *cold*
   before the rebuild finished — the next poll blocked synchronously again, and
   the spinner never cleared.

## User-facing change

* **Fast first paint.** On a cold cache the route now serves a fast
  local-Table-only payload immediately (`skip_enrichment`) and rebuilds the
  enriched payload (K8s refresh + external sync) in the background. The cheap
  per-cluster ARM health gate still runs, so a frozen running row is still
  tagged `stale` ("cluster stopped"). When the fast build has **no** local rows
  the route falls through to the full synchronous build, so external /
  sibling-only jobs are never hidden behind an empty list.
* **No recurrence.** The jobs-list cache no longer drops an entry to cold while
  a background rebuild is in flight — it keeps serving the (very) stale payload
  until the rebuild lands. The stale ceiling was widened (70 s → 600 s) and the
  single-flight/retention window decoupled (60 s → 1800 s crash-safety bound).

Net effect: the jobs list appears within a poll cycle and refreshes as the
background rebuild completes; statuses and external rows are at worst one
rebuild cadence behind (the same eventual consistency SWR already relied on).

## API / IaC diff summary

* [api/routes/blast/jobs.py](../../../api/routes/blast/jobs.py)
  * `_compute_blast_jobs_response(..., skip_enrichment: bool = False)` — skips the
    per-active-job K8s refresh and the external OpenAPI discovery/sync when set.
  * `blast_jobs_list` cold path: fast local-first build + background full
    rebuild, with an empty-local fall-through to the full synchronous build.
* [api/services/blast/jobs_list_cache.py](../../../api/services/blast/jobs_list_cache.py)
  * `JOBS_LIST_CACHE_STALE_TTL_SECONDS` 70 → 600.
  * `_JOBS_LIST_REVALIDATE_TTL_SECONDS` 60 → 1800 (crash-safety + retention bound).
  * `jobs_list_cache_get_swr` retains a past-ceiling entry while a revalidation
    is actively in flight instead of dropping it to cold.

No IaC, sidecar, or frontend changes.

## Validation

* `uv run pytest -q api/tests` — 3945 passed, 3 skipped.
* `uv run ruff check api` — clean.
* New/updated tests:
  * `api/tests/test_jobs_list_cache.py::test_swr_retains_entry_while_revalidation_inflight`
  * `api/tests/test_jobs_list_cache.py::test_swr_inflight_retention_expires_with_slot_ttl`
  * `api/tests/test_blast_jobs_routes.py::test_jobs_list_swr_serves_stale_and_revalidates` (rewritten for the cold fast-path contract)
  * `api/tests/test_blast_jobs_routes.py::test_jobs_list_cold_empty_local_falls_through_to_full_build`
</content>
</invoke>
