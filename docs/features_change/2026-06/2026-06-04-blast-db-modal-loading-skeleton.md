# BLAST Databases modal — loading skeleton

**Date:** 2026-06-04
**Area:** BLAST Databases card (Storage) — modal loading state

## Motivation

When the BLAST Databases modal opened, the database list query
(`dbQuery`) was still in flight while the catalog rendered immediately.
Because the downloaded-state map (`downloadedDbs`) is empty until that
fetch resolves, every catalog row fell through to the "not yet
downloaded" branch and rendered an **actionable** `Get` (Download)
button — and the Update / Delete affordances looked reachable too. A
user could click `Get` against a database that was, in fact, already
staged, simply because the real state had not arrived yet.

## User-facing change

- While the database list is loading for the first time, the modal now
  renders **animated shimmer skeleton rows** instead of the catalog with
  live buttons. No Download / Update / Delete action is clickable until
  the real downloaded-state is known.
- The custom-input download row is disabled during that initial load for
  the same reason.
- Once the list resolves (success **or** error) the skeleton clears and
  the normal catalog — including any degraded / private-network banners —
  renders exactly as before.

The skeleton reuses the existing `.skeleton` shimmer class (glassmorphic,
muted, 1.5 s ease-in-out) and mirrors the real row's `20px 1fr auto`
grid so the layout does not jump when data arrives.

## Update button verification

Audited the Update flow end-to-end as part of this change; no fix was
required:

- Row `Update` button (shown only when `hasUpdate` = downloaded + update
  available + not already updating) → `onUpdate()` →
  `setConfirmUpdateDb(db.value)` → `BlastDbUpdateConfirm` dialog →
  `startUpdate` → `handleUpdate` → `handleDownload(name, "update")` →
  `monitoringApi.prepareBlastDb(...)`.
- During the in-flight generation swap the row shows the
  `Updating · X%` chip (`update_in_progress`) plus a Cancel button, and
  `hasUpdate` correctly suppresses a second Update button until the swap
  finishes.

## API/IaC diff summary

None — frontend-only render change.

- `web/src/components/cards/storage/BlastDbRow.tsx`: new exported
  `BlastDbRowSkeleton` placeholder component.
- `web/src/components/cards/storage/BlastDbModal.tsx`: derive
  `dbInitialLoading = dbQuery.isLoading`; render skeleton rows during the
  initial load; gate the empty-state message, catalog list, and custom
  input on `!dbInitialLoading`.

## Validation evidence

- `cd web && npx tsc --noEmit` — clean.
- `cd web && npm run build` — built in ~12 s, no errors.
- `cd web && npm test -- --run` — 617 tests passing (69 files).
