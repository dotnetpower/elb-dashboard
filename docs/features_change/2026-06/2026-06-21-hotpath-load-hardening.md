---
title: Hot-path load hardening — chunked history lookup, cached job list, streamed export
description: Reduce CPU/memory load as job count and request rate grow — chunk get_history_for_jobs, cache /api/monitor/jobs, raise me/AKS cache TTLs, stream result export.
tags:
  - operate
  - blast
---

# Hot-path load hardening (chunked history, cached job list, streamed export)

## Motivation

Follow-up to the `get_many` CPU-storm fix: a full-codebase audit of CPU/memory
risks that worsen as job count grows or request rate spikes. The audit confirmed
the genuinely load-bearing problems are a small set of request-path hot spots;
several other suspected items turned out to already be bounded/cached. This
change ships the high-value, low-risk fixes; the remainder are deferred with
explicit rationale (see "Deferred" below).

## User-facing change

None functionally. Lower steady-state CPU/Table I/O on the polling hot paths and
lower peak memory on large result exports.

## Code changes

1. **Chunk `get_history_for_jobs`** — [api/services/state/repository.py](../../../api/services/state/repository.py)
   The PartitionKey-OR filter is now chunked into `_GET_MANY_FILTER_CHUNK` (50)
   id batches, identical to the `get_many` fix. Prevents the same over-length
   OData filter → HTTP 400 → swallowed → silent failure class. The audit route
   caps input at 20 today, but the function contract accepts any list.
   Regression test: `test_get_history_for_jobs_chunks_large_id_set`.

2. **SWR cache for `/api/monitor/jobs`** — [api/routes/monitor/jobs.py](../../../api/routes/monitor/jobs.py)
   The list route was uncached: every poll re-ran `_list_recent_sorted` (full
   filtered-set scan up to `JOBSTATE_LIST_SCAN_CAP=5000` + in-memory sort). Now
   served through `monitor_cache.cached_snapshot` (10 s TTL), keyed per caller
   (or a single `shared` bucket under shared-visibility) exactly like the
   message-flow card. The full scan now runs at most once per 10 s window
   regardless of tab count. Response gains a `cache` meta field (additive,
   consistent with every other monitor card; the SPA reads only `data.jobs`).

3. **`/api/me` subscription cache TTL 60 s → 300 s** — [api/routes/me.py](../../../api/routes/me.py)
   Visible-subscription listing barely changes mid-session; the list is already
   capped at 100. Longer TTL cuts the `subscriptions.list()` ARM enumeration
   frequency on large tenants.

4. **AKS subscription-wide list TTL 30 s → 60 s** — [api/routes/monitor/aks.py](../../../api/routes/monitor/aks.py)
   The subscription-wide ARM list (enumerate + deserialize every managed
   cluster) is the heaviest AKS read; 60 s halves its poll frequency on large
   subscriptions. Lifecycle transitions still settle promptly because the SPA
   passes `fresh=true` during a start/stop, bypassing the cache. RG-scoped reads
   stay at 30 s.

5. **Stream result export (JSON/CSV/TSV)** — [api/routes/blast/results_export.py](../../../api/routes/blast/results_export.py)
   `json.dumps(all_hits)` and `csv.StringIO` previously materialized the whole
   export (~50 MB for a 50K-hit job) a second time on top of the already-parsed
   `all_hits` list. New `_stream_json_export` / `_stream_delimited_export`
   generators yield incrementally, removing the duplicate serialization buffer
   (peak ≈ one row instead of the full file). Output is JSON/CSV-equivalent.

## Deferred (with rationale)

- **time-index flip ON** — the bounded time-ordered index is the root fix for
  the full-scan listings, but `time_index_enabled()` ships default-OFF per
  charter §12a Rule 4 (new behaviour is default-OFF; flipping is a separate PR
  after a dogfood cycle + backfill). `/api/monitor/jobs` caching is the interim
  mitigation. Flip remains a deploy-time decision.
- **message-flow `include_payload=False`** — `submission_source` and
  `query_size` live only in `payload_json` (not summary columns), so a naive
  flag flip breaks producer-lane classification. A safe 2-stage fetch (summary
  scan + payload backfill for rendered rows only) is a separate, regression-risky
  change; the card is already protected by the 30 s monitor cache.
- **reconcile_time_index batch writes** — Azure Table transactions require a
  single PartitionKey per batch (index rows are keyed per owner bucket), forcing
  owner-grouped buffering; the task is hourly and a no-op while the index is OFF.
- **web_blast_parity `iterparse`** — `parse_summary` is called only from tests,
  not any production route/task; not a runtime hot path.
- **merge-sharded-results top-N** — runs in the `terminal` sidecar (not the api
  load surface), is result-accuracy-critical, and needs a terminal redeploy;
  warrants a focused, separately-validated change.
- **blob_io `b"".join` buffer** — bounded at the 10 MiB read cap and callers
  need the whole text; low value.

## Validation

* `uv run pytest -q api/tests/test_state_repo.py -k "get_history or get_many"` — 3 passed.
* `uv run pytest -q api/tests` — 4136 passed, 3 skipped.
* `uv run ruff check api/` — clean.
* Frontend uses `/monitor/jobs/{id}` (detail) only, not the list route, so the
  additive `cache` field has no SPA impact.
