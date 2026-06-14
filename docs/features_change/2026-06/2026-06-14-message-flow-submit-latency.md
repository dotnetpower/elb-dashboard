---
title: BLAST jobs surface on the Message Flow card right after submit
description: Invalidate the monitor + external-jobs read caches on BLAST submit and tighten the Message Flow idle poll so a new job appears in seconds, not up to ~2 minutes.
tags:
  - blast
  - ui
---

# 2026-06-14 — BLAST jobs appear on Message Flow right after submit

## Motivation

Jobs submitted from **New Search** or the **OpenAPI `/v1/jobs`** path appeared on
the dashboard **Message Flow** card much later than they were actually running.
The producer was not enqueuing to Service Bus late — the delay was entirely on the
dashboard's **read/display** side, where three caches stacked:

| Layer | TTL | Affects |
| --- | --- | --- |
| External `/v1/jobs` list cache (`_external_list_jobs_cached`) | ~70 s | OpenAPI-plane jobs (not yet in the jobstate Table) |
| Monitor snapshot cache (`cached_snapshot` for `/api/monitor/message-flow`) | ~30 s | every path |
| Frontend idle poll (`MessageFlowCard` `refetchInterval`) | 20 s | every path; worst for the *first* job |

No submit path invalidated any of these (`invalidate_monitor_snapshot_prefix` was
only called by AKS routes; `_reset_external_jobs_cache` only on cancel/delete), so
a new job waited out all of them. Worst case: ~50 s for a New Search job and
~120 s for an OpenAPI job.

## User-facing change

* A BLAST submit now drops the read-side caches that gate the Message Flow card,
  so the job surfaces on the next card poll instead of waiting out the ~30 s /
  ~70 s server caches. This applies to the three dashboard-visible submit paths:
  `POST /api/blast/jobs` (local Celery), `POST /api/blast/jobs` with inline
  `query_fasta` (OpenAPI plane), and `POST /api/v1/elastic-blast/submit`.
* The Message Flow card's idle poll cadence tightened from 20 s to 10 s, so a
  brand-new job — which appears while the queue still looks idle — surfaces
  within one short poll. When nothing changed the poll is a cheap server
  cache hit (30 s TTL).
* Net effect: a New Search / OpenAPI-via-dashboard job appears on the card within
  a few seconds (next ≤10 s poll), instead of up to ~50–120 s.

## Scope / limitation

A BLAST job submitted **directly** to the sibling OpenAPI plane (bypassing the
dashboard entirely) is still discovered via the periodic external-jobs sync, so it
can take up to the discovery-cache window (~70 s) to appear. The dashboard cannot
invalidate on a submit it never sees; lowering that discovery TTL globally would
spam the OpenAPI `/v1/jobs` endpoint, so it is intentionally unchanged. All
dashboard-driven submits (New Search, API Reference Try-It, `/api/v1/elastic-blast/submit`)
get the fast path.

## API / IaC diff summary

No API surface or IaC change. Internal only:

* `api/routes/blast/submit.py` — new best-effort `_invalidate_message_flow_caches()`
  helper (resets the external-jobs cache + invalidates the `monitor:message-flow`
  snapshot prefix); called after a successful submit on both `/api/blast/jobs`
  branches.
* `api/routes/elastic_blast.py` — `/api/v1/elastic-blast/submit` calls the same
  helper after a successful submit (lazy import to avoid an import cycle).
* `web/src/components/cards/MessageFlow/MessageFlowCard.tsx` — idle
  `refetchInterval` 20 s → 10 s (active cadence unchanged at 8 s).

## Validation evidence

* `uv run pytest -q api/tests` → **3571 passed, 3 skipped**.
* New tests in `api/tests/test_message_flow_cache_invalidation.py`:
  * helper drops every `monitor:message-flow:*` snapshot + resets the external
    cache, leaves unrelated monitor snapshots intact;
  * helper is best-effort (a reset failure does not propagate);
  * `/api/v1/elastic-blast/submit` invalidates the seeded message-flow snapshot.
* `uv run ruff check` clean on all three changed Python paths.
* `npm run build` (web) succeeds; frontend MessageFlow tests (44) pass.
