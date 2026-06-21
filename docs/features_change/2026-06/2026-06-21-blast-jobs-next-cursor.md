---
title: BLAST jobs list — real keyset next_cursor over the time-ordered index
description: >-
  /api/blast/jobs now serves a true next_cursor (keyset of the last displayed
  row) off the jobstate time-ordered index, threaded through the SWR cache.
tags:
  - blast
  - architecture
---

# BLAST jobs list — real `next_cursor` (#50 AC4 / #51 local half)

## Motivation

The jobs-list `page` envelope already reported an honest `has_more` via a
fetch-one-extra probe, but `next_cursor` was always omitted — the route fetched
local rows through `list_for_owner` / `list_all` and discarded the index cursor.
With the time-ordered index (#50) in place, the route can now emit a real
continuation token so a client can page past the first window without re-reading
from the top.

## User-facing change

- `GET /api/blast/jobs` accepts a new optional `cursor` query parameter.
- When the time-index flag (`JOBSTATE_TIME_INDEX_ENABLED`) is on and the listing
  is owner- or all-scoped, the response `page.next_cursor` carries the **keyset
  of the last displayed row** (`encode_cursor(row_key(created_at, job_id))`).
- Passing that `cursor` back returns the next page with **no overlap and no
  gaps**. An expired/garbage cursor degrades gracefully to the first page (the
  `decode_cursor` fail-closed validation already rejects malformed tokens).
- Scoped listings (filter by cluster / subscription / resource group) stay
  first-page-only and report `next_cursor` omitted — the scope columns are
  mutable (`update()` rewrites them), so they cannot key the immutable index.
- Flag-off path is unchanged: `next_cursor` is omitted exactly as before.

## Why the keyset of the *displayed* row, not the fetch cursor

The route merges external OpenAPI `/v1/jobs` rows into the local set. Those rows
are upserted into the local Table (and therefore the time index), so the local
index is the effective combined cursor. Using the last *displayed* row's keyset
(rather than the local fetch cursor) keeps the boundary correct across the
merge: on a cursor page the local index page is filtered `RowKey gt cursor`, and
external rows at-or-newer-than the boundary (already shown on a previous page)
are dropped before merge so keyset pagination never duplicates.

## API / IaC diff summary

- `api/services/blast/jobs_list_cache.py` — `jobs_list_cache_key` gains a
  `cursor` field so each cursor page is a distinct SWR cache entry.
- `api/routes/blast/jobs.py` — `cursor` threaded through the route signature,
  the SWR cache key, both background-revalidate dispatches, and
  `_compute_blast_jobs_response`, which now (a) reads `list_owner_page` /
  `list_all_page` for cursor pages, (b) drops external rows newer-than-or-equal
  to the cursor boundary, and (c) computes `next_cursor` from the last displayed
  row.
- `api/services/response_contracts.py` — `build_page` docstring updated (the
  `next_cursor` field is now served, not reserved).
- No IaC change.

## Validation

- `uv run pytest -p no:xdist -o addopts="" api/tests/test_blast_jobs_routes.py` —
  24 passed, including 4 new cursor tests:
  `test_jobs_list_emits_next_cursor_keyset_of_last_row`,
  `test_jobs_list_cursor_page_continues_without_overlap`,
  `test_jobs_list_no_next_cursor_when_index_disabled`,
  `test_jobs_list_scoped_listing_has_no_cursor`.
- `uv run pytest -p no:xdist -o addopts="" api/tests/test_jobs_list_cache.py` —
  11 passed, including `test_cursor_distinguishes_cache_key`.
- `uv run pytest -p no:xdist -o addopts="" api/tests/test_jobstate_time_index.py`
  — 25 passed (index write/read invariants unchanged).
- `uv run ruff check api` — clean.

> The scoped canonical integration tests in `test_external_blast_api.py` hang
> locally (local `az` credentials trigger real ARM discovery timeouts), so the
> cursor logic is covered by the mocked route tests above, which stub the
> external sync and state repo.

## Out of scope / follow-up

- `list_for_scope` stays on the bounded `_list_recent_sorted` scan by design
  (mutable scope columns cannot key the immutable index) — #50 AC1's scope
  sub-item is intentionally a documented scan, pending maintainer decision.
- Folding a *sibling* `/v1/jobs` cursor into the combined token (#51 AC2) is
  blocked until upstream `dotnetpower/elastic-blast-azure` adds `cursor` support
  to `/v1/jobs`; today external rows reach the cursor via the local-Table sync.
- The SPA does not yet consume `next_cursor` (no infinite-scroll / load-more);
  the field is additive and ready for that frontend work.
