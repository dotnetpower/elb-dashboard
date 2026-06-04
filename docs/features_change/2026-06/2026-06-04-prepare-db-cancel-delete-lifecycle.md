# prepare-db Cancel/Delete lifecycle completion

**Date:** 2026-06-04
**Area:** BLAST Databases card (Storage) — prepare-db lifecycle UI + API

## Motivation

The prepare-db (BLAST DB staging) flow could create and update a database but
offered no by-the-book way to *undo*:

- **Cancel** was only rendered while a fresh copy was active (`copyActive`).
  Once a copy flipped to the `update_in_progress` "Updating …" badge there was
  no Cancel button, so an in-flight generation swap (e.g. a stuck AKS-fanout
  update) could not be aborted from the UI.
- **Delete** did not exist at all — no backend route, no button. A `partial` /
  `init_failed` / `cancelled` leftover, or a fully-staged database the user no
  longer wanted, could only be removed by hand against Storage. That left
  staged shard blobs (and any leftover AKS Job/ConfigMap) orphaned.

This closes the resource lifecycle: every prepare-db state now has an
always-reachable Cancel (in-flight) or Delete (terminal) action.

## User-facing change

- **Cancel now also shows during an in-flight update** (the "Updating · X%"
  badge gains a Cancel button), so a stuck generation swap can be aborted
  without waiting out the stale-recovery window.
- **New Delete action** (Trash2 icon) on:
  - a **Ready** database (next to the "Ready" chip), and
  - a **partial / cancelled** leftover (next to the Retry/Get button).
- Delete opens a danger `ConfirmDialog` warning that all staged shard blobs +
  metadata (and any AKS prepare-db Job) are permanently removed and the
  database would have to be re-downloaded.
- Delete is **refused (409)** while a copy is genuinely in flight
  (`copy_status.phase ∈ {queued, copying}` or `update_in_progress`) — the user
  is told to Cancel first, so a Delete never races a live azcopy fan-out.

## API / IaC diff summary

New route (mirrors the existing `prepare_db_cancel`):

```
POST /api/storage/prepare-db/{db_name}/delete
body: { subscription_id, storage_resource_group, account_name }
-> { ok, db_name, deleted, errors, metadata_deleted, aks_job_deleted }
```

Behaviour, in order:
1. Read metadata; **409** if a copy is in flight (`queued`/`copying`/
   `update_in_progress`).
2. Delete any `aks_job_ref` Job + ConfigMap via the existing idempotent
   `delete_prepare_db_job` (404 = success).
3. List + delete every blob under `{db_name}/`, then delete
   `{db_name}-metadata.json` last (a mid-delete crash leaves re-deletable
   state rather than orphaned blobs).
4. Invalidate the merged display-metadata cache via
   `notify_blast_db_metadata_changed`.
5. Best-effort audit row via `record_db_op(op="prepare_db_delete", …)`.

Frontend:
- `monitoringApi.deletePrepareBlastDb(...)` typed client.
- `useBlastDb.handleDelete(dbName)` (mirrors `handleCancel`: clears in-progress
  map + toast state, refetches).
- `BlastDbRow` gains an optional `onDelete?` prop + Trash2 buttons; the
  `isUpdating` branch gains a Cancel button.
- `BlastDbModal` adds `confirmDeleteDb` state + a danger Delete `ConfirmDialog`
  and wires `onDelete` / `handleDelete`.

No IaC change.

## Validation evidence

- `uv run pytest -q api/tests/test_prepare_db_delete_route.py` — **5 passed**
  (ready cleanup, in-flight 409, update-in-progress 409, partial+aks_job_ref
  Job delete, idempotent-when-absent).
- `uv run pytest -q api/tests -k prepare_db` — **86 passed**.
- `uv run pytest -q api/tests` — **2647 passed**, 3 skipped (1 unrelated
  flake in `test_terminal_exec.py::test_run_truncates_stdout_above_cap` that
  passes in isolation).
- `uv run ruff check api` — clean.
- `cd web && npm run build` — clean; `npm test -- --run` — **616 passed**.
- `npx tsc --noEmit` — clean.
