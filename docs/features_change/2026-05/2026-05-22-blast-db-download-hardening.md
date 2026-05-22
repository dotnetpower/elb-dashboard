# BLAST DB download hardening — version preview, honest copy status, atomic promotion

**Date**: 2026-05-22
**Scope**: backend + frontend

## Motivation

The DB download surface had 20 known issues (see chat critique), the loudest
being:

- No way to see NCBI snapshot info for a DB BEFORE clicking Download — users
  could not tell whether a fresh NCBI generation existed, or whether the DB
  was even present in the current S3 snapshot.
- Server-side copy completion was inferred from a 90 %-of-files heuristic on
  the SPA; partial copies silently flipped to "Ready" and broke later BLAST
  submits.
- `/databases/check-updates` compared a single bucket-wide `latest-dir` value,
  causing false-positive "update available" badges on every snapshot rotation.
- `start_copy_from_url` failures were never observed — `copy.status='failed'`
  was invisible.
- `prepare-db` had no per-(account, db) lock, no stale-flag recovery, and no
  ETag-aware metadata writes.

## User-facing change

- Catalog rows in **BLAST Databases** modal now show live NCBI snapshot info
  (snapshot id, file count, estimated size) BEFORE the user clicks Get.
- Rows for DBs missing from the current S3 snapshot show a clear "Not in
  current NCBI snapshot" warning instead of letting the Download button fail
  mid-copy with a 404.
- A "Get" button on a partially copied DB becomes "Retry" and shows the
  failed/aborted/pending counts; download-result toast now includes the
  authoritative error reason from the backend.
- The header "downloads available" badge no longer fires every time NCBI
  rotates `latest-dir` — it now uses a per-DB ETag signature.
- Custom DB name input validates client-side (`[A-Za-z0-9_.-]{1,64}`) and
  shows an inline hint instead of a backend 400 toast.
- DB Versions tab dropped the always-`—` "By" and "Notes" columns.
- Elapsed timer now tracks the full server-side copy lifetime instead of
  resetting to 0 the moment the POST returned.

## API / IaC diff summary

### New routes

- `GET /api/blast/databases/{db_name}/preview` — dry-run NCBI snapshot
  summary (snapshot id, file count, volume count, total bytes estimate,
  last-modified, signature ETag, files sample, `available` flag, hint).

### Changed routes

- `GET /api/blast/databases/check-updates` now accepts optional
  `subscription_id` / `storage_account` / `resource_group` query params and
  returns a per-DB `updates_available` list keyed by NCBI ETag instead of the
  legacy `source_version != latest_version` heuristic. Back-compat preserved
  when storage scope is omitted.
- `POST /api/storage/prepare-db`:
  - Acquires a per-(account, db) lock; returns 409 instead of spawning a
    second daemon.
  - Recovers from `update_in_progress=true` markers older than 2 h (crashed
    previous daemon).
  - Polls `BlobProperties.copy.status` for every staged blob; partial
    completion records `copy_status.phase = "partial"` + `failed_files` and
    **does NOT promote `source_version`**.
  - Atomic promotion: `source_version` only set when every copy reaches
    `success`.
  - Metadata writes use ETag / `If-Match` with retry so concurrent shard /
    warmup writers can't clobber unrelated fields.
  - 403 from NCBI is now distinguished from 404 (DB not in snapshot) so the
    SPA can show the right hint.

### New services

- `api/services/ncbi_catalogue.py` — `preview_database`,
  `database_update_signature`, `RE_DB_NAME`. Cached `(snapshot, db)` HEAD
  results for 30 min.

### Hardened helpers

- `api/routes/storage/common.py`:
  - `NcbiAccessDenied` / `NcbiUnavailable` exception types.
  - `_resolve_latest_dir` and `_list_keys` no longer cache empty/failed
    responses (mid-publish snapshot can no longer poison the 1 h cache).
  - Explicit 403 vs 5xx classification.

### Frontend

- `web/src/api/blast.ts`: new `blastApi.previewDatabase`, enriched
  `blastApi.checkUpdates`, new `BlastDatabase.copy_status` / `failed_files` /
  `signature_etag` fields.
- `web/src/components/cards/storage/useBlastDb.ts`: completion detection now
  uses `copy_status.phase === "completed"` instead of the 90 % heuristic;
  exposes `isDbReady`, `updatesAvailableByDb`; elapsed timer ticks for the
  full in-progress lifetime.
- `web/src/components/cards/storage/useDbPreviews.ts`: batched per-DB preview
  query (`useQueries`, 10 min stale time).
- `web/src/components/cards/storage/BlastDbRow.tsx`: surfaces preview info,
  shows partial-copy badge, disables Get when DB is not in current snapshot,
  retry button on partial state.
- `web/src/components/cards/storage/BlastDbCustomInput.tsx`: client-side
  regex validation + inline error hint.
- `web/src/pages/tools/tabs/DbVersionsTab.tsx`: dropped dead `created_by` /
  `notes` columns.

## Validation evidence

```
$ uv run pytest -q api/tests/test_prepare_db_hardening.py \
    api/tests/test_blast_databases_preview.py \
    api/tests/test_blast_databases_check_updates.py \
    api/tests/test_ncbi_catalogue.py \
    api/tests/test_storage_common_cache.py \
    api/tests/test_blast_databases_versions.py \
    api/tests/test_blast_databases_warmup_plan.py \
    api/tests/test_route_contracts.py
35 passed in 3.13s

$ cd web && npm run build
✓ built in 7.26s
```

Full backend suite: `1108 passed, 1 failed` — the single failure is the
pre-existing `test_facade_contract_covers_all_string_target_monkeypatches`
flagging an untracked `api.tasks.upgrade.remote_tags.fetch_release_tags`
target, unrelated to this change (verified via `git stash` — same failure on
the untouched tree).

## Out of scope

- FTP fallback for DBs missing from the S3 mirror is **not** implemented;
  the SPA now surfaces the "FTP-only" hint instead, so the user can act on
  it. Auto-pulling from FTP would require additional work (different layout
  + no `start_copy_from_url` path).
