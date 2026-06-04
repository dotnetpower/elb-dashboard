# BLAST Databases — immediate Cancel/Delete feedback

**Date:** 2026-06-04
**Area:** BLAST Databases card (Storage) — prepare-db Cancel/Delete UX

## Motivation

Clicking **Cancel** on an in-flight prepare-db copy (or **Delete** on a
staged database) gave no feedback until the network request resolved a
few seconds later. Worse, `handleCancel` cleared the result banner
*first* and only set the "Cancelled …" banner *after* the await — so for
the whole round trip the row still showed the live progress / "Updating"
chip with an active Cancel button and no banner. The action looked
ignored ("한참 뒤에 바뀌어 Cancel 이 안 된 것처럼 보인다").

## User-facing change

The moment Cancel / Delete is confirmed, before the network call:

- A **pending banner** appears at the top of the modal in a calm accent
  tone with a spinner — "Cancelling — aborting the remaining pending
  copies…" / "Deleting — removing the staged blobs…". It does not
  auto-dismiss; it is replaced by the terminal success/error banner when
  the request resolves.
- The row's **Cancel button** turns into a disabled "Cancelling…"
  spinner; the **Delete button** turns into a disabled spinner. This
  prevents a double-click and makes the in-flight state obvious.

Once the request resolves the banner flips to the existing
success/error result and the row refreshes from the refetched DB list as
before.

## API/IaC diff summary

None — frontend-only render/state change.

- `web/src/components/cards/storage/useBlastDb.ts`: `DownloadResult.type`
  gains `"pending"`; new `pendingAction: Map<db, "cancel" | "delete">`
  state; `handleCancel` / `handleDelete` set the pending banner + flag
  before the await and clear the flag in `finally`. `pendingAction` is
  exposed on the hook return.
- `web/src/components/cards/StorageDownloadResultBanner.tsx`: renders the
  `pending` type (accent colour, `Loader2` spinner, no auto-dismiss).
- `web/src/components/cards/storage/BlastDbRow.tsx`: new
  `isCancelling` / `isDeleting` props drive a disabled spinner on both
  Cancel branches (update + copy) and both Delete branches (Ready +
  partial/cancelled leftover).
- `web/src/components/cards/storage/BlastDbModal.tsx`: passes
  `isCancelling` / `isDeleting` derived from `pendingAction` per row.

## Validation evidence

- `cd web && npx tsc --noEmit` — clean.
- `cd web && npm run build` — built in ~7 s, no errors.
- `cd web && npm test -- --run` — 617 tests passing (69 files).
