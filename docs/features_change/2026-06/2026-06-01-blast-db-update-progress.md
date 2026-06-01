# BLAST DB update shows live copy progress, not just "Updating"

## Motivation

On a deployed control plane, updating a large BLAST database (e.g. `core_nt`)
only rendered a bare **"Updating"** badge with no progress. The progress text
and bar were gated on `isCopying && inProgressInfo`, which is populated **only**
for copies started in the current browser tab via `handleDownload`. After a page
reload — or for an update initiated server-side / in another tab — that local
state is gone, so the row showed no moving progress even though the server was
actively copying. The 10s auto-refetch was likewise gated on the local
`inProgress` map, so the row would not advance on its own.

## User-facing change

- The DB row now derives copy progress from the server metadata
  (`copy_status`) whenever the server reports an in-flight copy
  (`copy_status.phase` of `queued` / `copying`, or `update_in_progress`),
  in addition to the existing current-tab path.
- Progress is **unit-aware**: server-side copies report per-file counts
  (`Copying N / M files`); AKS-fanout copies (used for large DBs like
  `core_nt`) have no per-file `success`, so the row falls back to
  pod/shard counts (`Copying N / M shards`) so the bar still moves.
- Elapsed time is taken from `copy_status` / `update_started_at` when the
  local start time is unavailable (e.g. after a reload).
- The right-side **"Updating"** chip now appends `· {pct}%` when a total is
  known, and the row icon / shimmer / catalog-estimate hint reflect the
  server-reported copy state for consistency.
- The DB list keeps polling (10s) while the server reports any in-flight copy,
  so progress advances after a reload without requiring a current-tab copy.

## API / IaC diff summary

Frontend-only. No backend, API contract, or IaC changes.

- `web/src/components/cards/storage/useBlastDb.ts`
  - `DownloadedDbMeta.copy_status` extended with optional AKS pod fields
    (`mode`, `active_pods`, `succeeded_pods`, `failed_pods`, `shard_count`) —
    additive and optional, backward-compatible.
  - 10s polling effect now also fires when `serverCopyActive` is true
    (derived from `update_in_progress` / `copy_status.phase`).
- `web/src/components/cards/storage/BlastDbRow.tsx`
  - Progress computation, progress text, and progress bar are driven by
    `copyActive` (local OR server copy) and a unit-agnostic
    files-vs-shards progress model.

## Validation evidence

- `npx tsc --noEmit` — clean.
- `npx eslint src/components/cards/storage/BlastDbRow.tsx src/components/cards/storage/useBlastDb.ts` — clean.
- `npx vitest run blastDbProgress.test.ts blastDbReady.test.ts warmupSection/helpers.test.ts` — 24 passed.
- `npm run build` — built in ~6.7s, no errors.
