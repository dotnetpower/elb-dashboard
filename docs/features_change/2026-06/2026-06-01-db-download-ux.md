---
title: BLAST database download progress, ETA, and cancel confirmation
description: The BLAST database download (prepare-db) row now shows a flicker-free monotonic file count, a live ETA projected from observed throughput, and a confirmation dialog before an in-flight copy is cancelled.
tags:
  - ui
  - blast
---

# BLAST database download progress, ETA, and cancel confirmation

## Motivation

The BLAST database modal's per-DB download row (the hardened prepare-db copy
flow) had three rough edges the user called out:

1. **The "Copying X / Y files" counter jumped up and down.** The displayed
   numerator was `copy_status.success ?? file_count ?? 0`. Whenever the
   server-side copy metadata momentarily lacked `copy_status.success`, the UI
   fell back to the live blob-listing `file_count` — a different, larger number
   that fluctuates while copies are mid-flight — so the count visibly flickered
   between the two sources.
2. **Cancelling was a one-click action with no confirmation.** The Cancel
   button called the abort endpoint immediately, with no "are you sure" step.
3. **The estimate was static.** The row showed the catalog's fixed
   `est. ~2-4 hours` regardless of how the copy was actually progressing.

## User-facing change

- **Flicker-free count.** The copied-file count now trusts only
  `copy_status.success` during an active copy (no `file_count` fallback) and is
  clamped to be monotonic non-decreasing within a single copy session, so it
  only ever increases.
- **Live ETA.** The static `est. <catalog>` text is replaced by a dynamic
  estimate projected from observed throughput (`success / elapsed`). It shows
  `estimating…` until throughput stabilises, then tightens to `~7m left` /
  `~1h 5m left` as the copy runs.
- **Cancel confirmation.** Clicking **Cancel** now opens a confirmation dialog
  ("Cancel download of <db>? Files already copied stay in place; only the
  remaining pending copies are aborted.") and only calls the abort endpoint
  after the user confirms.

> Note: the download still uses the **server-side** copy path (the `api` sidecar
> issues per-file `start_copy_from_url` S3→blob copies). The AKS-node `azcopy`
> fan-out path exists in the backend (`mode=aks`/`auto`) but the dashboard
> download button does not request it; wiring that mode into the UI is a
> separate change.

## Diff summary

- `web/src/components/cards/storage/blastDbProgress.ts` (new) — pure
  `formatDuration` / `formatEta` helpers projecting remaining time from
  copied/total + elapsed seconds.
- `web/src/components/cards/storage/blastDbProgress.test.ts` (new) — 8 unit
  tests for the duration/ETA math.
- `web/src/components/cards/storage/BlastDbRow.tsx` — monotonic copied-count via
  a `useRef` clamp, local copy-% computed from the monotonic count, live ETA
  label; dropped the `file_count` fallback and the static `db.estMinutes` from
  the copying line; removed the `copyProgress` prop (now computed locally).
- `web/src/components/cards/storage/BlastDbModal.tsx` — removed the
  flicker-prone `copyProgress` computation; added a `confirmCancelDb` state and
  a `ConfirmDialog` gate so Cancel requires confirmation; `onCancel` now opens
  the dialog instead of aborting directly.

No backend/IaC changes.

## Validation evidence

- `npx vitest run src/components/cards/storage/blastDbProgress.test.ts src/utils/blastDbReady.test.ts` → 18 passed.
- `npx vitest run src/components/cards/storage` → 19 passed (4 files).
- `npm run build` → clean (tsc + vite, built in ~6 s).
