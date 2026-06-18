---
title: Recent searches load-more (infinite scroll)
description: Recent searches now grows its list as the user scrolls (or clicks Load more), bringing back jobs beyond the most-recent 20 without blanking the list.
tags:
  - blast
  - user-guide
---

# Recent searches load-more (infinite scroll)

## Motivation

The previous change capped the Recent searches initial load to the most-recent
20 jobs for a fast first paint, but there was no way to reach older jobs — a
researcher with more than 20 searches saw their earlier runs effectively
disappear from the list. This adds a "load more" affordance that triggers on
scroll, so the full history stays reachable.

## User-facing change

* **Recent searches** now loads more jobs as you scroll toward the bottom of the
  list (an `IntersectionObserver` sentinel with a 400 px pre-fetch buffer). A
  **Load more** button is also rendered as the keyboard-accessible / no-observer
  fallback.
* The list no longer blanks to the loading skeleton while a larger page loads —
  the current rows stay on screen (`keepPreviousData`) and a small
  "Loading more searches…" indicator appears at the bottom.
* The trigger is only shown when the backend reports more rows
  (`page.has_more`); a workspace with ≤ 20 jobs sees no extra UI.

## How it works (interim until cursor pagination)

The page hook holds a `limit` that starts at 20 and grows by 20 each time
"load more" fires. The backend already returns the genuinely most-recent N rows
plus the `page.has_more` flag (shipped in the prior change), so bumping the
limit brings older jobs back in correct recency order. This is the interim
mechanism until the time-ordered secondary index + real `next_cursor` lands
(issue #50); the `page` envelope is already forward-compatible, so swapping to
cursor paging later is a localized change.

## API / IaC diff summary

* `web/src/pages/BlastJobs/JobsLoadMore.tsx` (new) — `IntersectionObserver`
  sentinel + fallback button; presentation/trigger only.
* `web/src/pages/BlastJobs/useBlastJobsState.ts` — stateful `limit` (resets on
  cluster pin change), `hasMore` / `isFetchingMore` / `loadMore` derived from
  `jobsQuery.data.page`.
* `web/src/pages/BlastJobs/BlastJobs.tsx` — renders `JobsLoadMore` after the
  grouped list.
* `web/src/hooks/useScopedBlastJobs.ts` — `placeholderData: keepPreviousData`
  so growing the limit doesn't blank the list.
* No backend or IaC change (the `limit` + `page.has_more` contract already
  shipped).

## Validation evidence

* `cd web && npm run build` — type-checks and builds clean.
* `cd web && npm test -- --run` — 900 passed.
* Note: takes effect on the live site only after a frontend deploy; not deployed
  here (maintainer's call per charter §13).
