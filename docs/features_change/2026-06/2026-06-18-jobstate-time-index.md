---
title: Time-ordered jobstate index (flag-OFF) for bounded most-recent listing
description: Optional jobstateindex secondary index lets list_for_owner read the most-recent N jobs as a bounded page instead of scanning up to 5000 rows; default OFF, legacy scan retained as fallback.
tags:
  - architecture
  - blast
---

# Time-ordered jobstate index — PR-1 (#50)

## Motivation

`/api/blast/jobs` initial load is slow on a long job history: the genuinely
most-recent ordering is computed in process — `_list_recent_sorted` reads the
full filtered set (bounded by `JOBSTATE_LIST_SCAN_CAP`, default 5000) from Azure
Table Storage and sorts by `created_at` in memory, because the table has no
server-side ordering and `jobstate` rows use a random-uuid PartitionKey. So even
`limit=20` scans up to 5000 rows.

## User-facing change

None by default. This ships the index machinery **default-OFF**
(`JOBSTATE_TIME_INDEX_ENABLED` unset). Production behaviour is byte-identical
until the flag is flipped, and the flag may only be flipped **after a completed
backfill** (an un-backfilled index would under-report old jobs). When enabled,
`list_for_owner` reads the genuinely-most-recent `limit` rows as an O(limit) page
instead of a 5000-row scan.

## Design — immutable index key

The index row is keyed only on fields that are set at create and **never mutated**
by `update()`:

- `PartitionKey = owner_oid` (or the `__shared__` sentinel for `owner_oid=""`).
- `RowKey = <inverted-ticks(created_at), 14-digit zero-padded>_<job_id>` so
  Azure's lexical RowKey order equals numeric order, newest first.

Because both inputs are immutable, an index row **never moves**. The only
mutations are **add on create** and **remove on soft-delete** (status →
`deleted`); ordinary status transitions (queued → running → completed) do **not**
touch the index. This collapses the consistency/concurrency surface: create is
already idempotent (`ResourceExistsError` swallow) and the index upsert is
idempotent on the same RowKey; soft-delete is idempotent (delete-already-deleted
is a no-op).

`list_for_owner`'s `owner_oid eq X or owner_oid eq ''` filter maps to exactly two
index partitions (the owner's bucket + the shared bucket); each is read
newest-first for `limit + 1` rows (the extra row is the honest `has_more` probe),
merged by RowKey, truncated, and the job rows are batch-fetched in order.

## API / IaC diff summary

- `api/services/state/time_index.py` (new): pure key/cursor helpers
  (`time_index_enabled`, `owner_bucket`, `row_key`, `encode_cursor`,
  `decode_cursor`, `build_index_entity`).
- `api/services/state/repository.py`:
  - `_index_client` / `_index_put` / `_index_delete` — flag-gated, best-effort
    (an index write failure logs + does not fail the create; the `jobstate` row
    stays the source of truth and the backfill reconciles).
  - `create` writes the index row, `update` removes it on soft-delete — both
    gated behind `time_index_enabled()`.
  - `list_owner_page(owner_oid, *, limit, include_payload, cursor)` returns
    `(rows, next_cursor)` via the index; `list_for_owner` uses it when enabled
    and **falls back to the legacy scan** on any index error or empty result.
  - `get_many` gains an optional `select` (additive) so the indexed read can
    skip `payload_json` for summary listings.
- `scripts/dev/backfill_jobstate_time_index.py` (new): idempotent migration that
  upserts an index row for every non-deleted `jobstate` row. Run (and verify)
  before flipping the flag. Supports `--dry-run`.

No Bicep / infra change: the feature reads `JOBSTATE_TIME_INDEX_ENABLED` from
env; absence = OFF = current behaviour.

## Out of scope (still open on #50)

- Wiring `next_cursor` through `_compute_blast_jobs_response` + the SPA (the
  paged method returns it; the route still uses fetch-one-extra `has_more`).
- `list_for_scope` / `list_all` index buckets (operator / dev paths kept on the
  legacy scan).
- External `/v1/jobs` cursor (tracked in #51).

## Validation evidence

- `uv run pytest -q api/tests/test_jobstate_time_index.py` — 15 passed:
  helper key/cursor math; create writes/omits the index row by flag;
  index-write-failure does not fail create (partial-failure path); newest-first
  pagination with a round-trippable cursor and no overlap/no gaps; owner+shared
  bucket merge; soft-delete removes the row; a status update does NOT move the
  row (immutable key); idempotent re-create keeps a single row; flag-ON read uses
  the index; empty index falls back to the legacy scan.
- `uv run pytest -q api/tests/test_persona_matrix.py api/tests/test_state_repo.py`
  — 73 passed (no regression; all new params default).
- Full suite `uv run pytest -q api/tests` — 3971 passed, 3 skipped.
- `uv run ruff check api` clean.
