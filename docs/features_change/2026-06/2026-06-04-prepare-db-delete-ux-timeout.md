---
title: Prepare-DB delete — batch removal, longer client timeout, and a clear "Deleting…" row state
description: Fix the misleading "Request timed out" error and the lingering "Get" button while a large BLAST DB (e.g. nt) is being deleted.
tags:
  - blast
  - ui
---

# Prepare-DB delete: batch removal + "Deleting…" row state

## Motivation

Deleting the `nt` BLAST DB (~4.8k shard blobs) surfaced two problems in the
dashboard:

1. **"nt: Request timed out"** — the delete route removed every blob with a
   *serial* `delete_blob` loop. Thousands of sequential round-trips routinely
   ran past the frontend's hard 30 s request window, so the browser aborted the
   call and showed a timeout error even though the backend kept deleting
   successfully.
2. **The "Get" button stayed visible** — because `nt` was in a `partial` phase,
   the row fell through to the not-downloaded "Get/Retry" branch. The
   `isDeleting` flag only swapped the trash icon to a spinner, so the Get button
   remained clickable *during* the delete, inviting a download-vs-delete race.

## User-facing change

- Deleting a large DB now completes in seconds instead of minutes, and returns a
  proper success (`Deleted — removed N blobs`) instead of a misleading timeout.
- While a delete is in flight the row shows a single, unambiguous
  **"Deleting…"** chip and no longer offers the Get/Retry button.

## API / IaC diff summary

- `api/routes/storage/prepare_db.py` — `prepare_db_delete` now removes shard
  blobs with Azure **batch delete** (`container.delete_blobs(*chunk,
  raise_on_any_failure=False)`) in chunks of ≤256, treating HTTP 202/200/404 as
  success and counting the rest as `errors`. Shard names are fully enumerated
  *before* any delete so listing and deleting never interleave (no
  pagination-during-mutation risk). A whole-batch failure falls back to
  per-blob deletes for just that chunk so one bad batch never strands the rest.
  **Partial-failure invariant:** when any shard delete fails (`errors > 0`) the
  metadata blob is **kept** (not deleted) so the row stays visible and
  re-deletable instead of leaking orphan blobs; the response gains
  `partial: true`. `partial` is also true when every shard was removed but the
  metadata blob delete itself failed (DB still listed). Response is otherwise
  backward compatible.
- `web/src/api/client.ts` — `FetchApiOptions` gains an optional `timeoutMs`;
  `fetchApi` forwards it to `fetchWithRetry`, and `api.post` accepts an optional
  `{ timeoutMs }` third argument. Default behaviour (30 s) is unchanged for all
  other callers.
- `web/src/api/monitoring.ts` — `deletePrepareBlastDb` requests a 180 s timeout
  and its response type gains the optional `partial` field.
- `web/src/components/cards/storage/useBlastDb.ts` — on a partial delete
  (`errors > 0` or `partial`) the hook warns ("Partial delete … Try Delete
  again") and leaves the row in place; the error/timeout path now also calls
  `dbQuery.refetch()` so a successful-but-timed-out delete reconciles against
  server truth instead of leaving a stale Get button. Any local copy-tracking
  (`inProgress`) for the DB is cleared after every delete attempt so a stale
  "Copying … Ns" timer/poll never lingers on a just-deleted row.
- `web/src/components/cards/storage/BlastDbRow.tsx` — a high-priority
  `isDeleting` branch renders the "Deleting…" chip and hides the download button.

## Validation evidence

- `uv run pytest -q api/tests/test_prepare_db_delete_route.py` → 7 passed
  (added `test_delete_partial_failure_keeps_metadata` +
  `test_delete_metadata_failure_reports_partial`; mock `_FakeContainer`
  extended with `delete_blobs`, a `fail_names` set and `fail_metadata`).
- `uv run pytest -q api/tests` → 2723 passed, 3 skipped.
- `uv run ruff check api/routes/storage/prepare_db.py api/tests/test_prepare_db_delete_route.py` → clean.
- `cd web && npm run build` + `npx tsc --noEmit` → clean.
- `cd web && npm test -- --run src/components/cards/storage/ src/api/resilience.test.ts src/api/aks.test.ts` → 39 + 10 passed.

## Deployment note

This touches `api/` and `web/` only (no sidecar/Bicep change). It takes effect
live only after the api + frontend sidecars are redeployed; redeploy on request.
