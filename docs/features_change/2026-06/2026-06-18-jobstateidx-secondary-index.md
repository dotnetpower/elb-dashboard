---
title: "Secondary index for jobstate O(limit) listing"
description: "Add jobstateidx Azure Table secondary index so list_for_owner reads O(limit) rows instead of O(N) full-scan."
tags: [blast, operate]
---

## Motivation

`list_for_owner` performed a full table scan (O(N)) to find the most-recent `limit` rows, because Azure Table Storage does not support server-side ordering. On a busy installation with thousands of job rows, the scan consumed the entire `LIST_SCAN_HARD_CAP` budget (default 5000 rows), creating latency spikes on the SPA's 14-second poll cadence.

## User-facing change

- The jobs list page loads faster on installations with many jobs (O(limit) reads instead of O(N)).
- The `/api/blast/jobs` route accepts a new optional `cursor` query parameter for true cursor-based pagination across large job sets.
- The `page` envelope in the jobs-list response now includes `next_cursor` when more pages exist and a cursor-indexed read was used.

## API / IaC diff summary

- `api/services/state/repository.py` — new `jobstateidx` Azure Table secondary index:
  - `_idx_row_key` / `_parse_idx_cursor` / `_encode_idx_cursor` / `_merge_idx_rows` helpers
  - `_query_idx_partition` — reads one partition of the index
  - `list_for_owner_indexed(owner_oid, limit, *, cursor)` → `(rows, next_cursor, has_more)` — O(limit) read, both owner and shared (owner_oid='') partitions
  - `list_for_owner` — now tries `list_for_owner_indexed` first, falls back to `_list_recent_sorted` on any exception
  - `create()` — writes index entry after main write (best-effort)
  - `update()` — upserts index entry; deletes it on `status='deleted'`
- `api/routes/blast/jobs.py` — `blast_jobs_list` route adds `cursor: str = Query(default="")` and threads it through `_compute_blast_jobs_response`; cursor requests bypass the SWR cache
- `scripts/db/backfill_jobstate_idx.py` — one-shot idempotent migration script to backfill existing rows into the index

## Validation evidence

- `uv run pytest -q api/tests/test_state_repo.py` — 21 tests pass (3 new: `test_list_for_owner_includes_cluster_shared_rows`, `test_list_for_owner_falls_back_to_full_scan_when_index_raises`, `test_list_for_owner_indexed_returns_newest_first`)
- `uv run pytest -q api/tests/` — 3954 tests pass, 3 skipped
- `uv run ruff check api/` — clean

## Persona impact

| Persona | Change |
|---------|--------|
| owner / contributor | Jobs list loads faster; pagination cursor available |
| reader | Same as above |
| dev_bypass | No change |

## Notes for operators

Run the backfill script once after deploying to populate the secondary index for existing jobs:

```bash
uv run python scripts/db/backfill_jobstate_idx.py
# or --dry-run to preview
```

The index is best-effort: if a write fails, existing jobs remain visible via the full-scan fallback. The backfill is idempotent and can be re-run safely.
