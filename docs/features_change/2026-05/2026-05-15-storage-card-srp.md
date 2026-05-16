# StorageCard SRP split

## Motivation

`web/src/components/cards/StorageCard.tsx` was a 1273-line monolith mixing:
- Storage-account meta query + warning banners + container table
- BLAST database catalog rendering (rows, modal, custom-input, large-confirm)
- Download lifecycle state (in-flight / in-progress / locally-completed)

Single responsibility was violated four ways inside one file, which made it
hard to (a) reason about download state transitions and (b) tweak modal
layout without scrolling past the meta panel logic.

## User-facing change

None. Pure refactor — the rendered UI, data flow, query keys, refetch
intervals, polling cadence, and download "completion" threshold (90% of
expected files) are all preserved byte-for-byte from the original.

## File diff summary

```
web/src/components/cards/StorageCard.tsx        1273 -> 95   (coordinator)
web/src/components/cards/storage/                              (new folder)
  useBlastDb.ts                                  215         (hook: download lifecycle + queries)
  StorageWarnings.tsx                             92         (public + HNS warning banners)
  StorageMetaGrid.tsx                             68         (region/SKU/HNS/public 4-col grid)
  StorageContainersTable.tsx                      90         (container list table)
  BlastDbSection.tsx                             113         (inline header + chips + modal trigger)
  BlastDbModal.tsx                               353         (full popup contents)
  BlastDbRow.tsx                                 327         (single DB row inside the modal)
  BlastDbCustomInput.tsx                          61         (collapsed → inline custom DB input)
  BlastDbLargeConfirm.tsx                         67         (confirm block for Large category)
```

Total: 95 + 1386 = 1481 lines vs the original 1273. The growth is exclusively
explicit prop interfaces and per-module docstrings — no logic was duplicated.

## Boundary contracts (load-bearing)

- `useBlastDb` is the single source of truth for the three lifecycle maps
  (`downloading`, `inProgress`, `locallyDownloaded`) and the `downloadedDbs`
  merge that powers the UI. The 90%-of-expected-files completion heuristic
  and the 10s polling interval live there.
- `BlastDbSection` owns the inline summary and only surfaces `activeDownload`
  upward via `onDownloadingChange` so the parent `StorageCard` can keep its
  card-level shimmer on while a copy is running.
- `BlastDbModal` is a pure renderer that receives the `useBlastDb` return
  object as `state` — no extra fetching, no parallel state, no duplication of
  the lifecycle logic.

## Validation evidence

- `cd web && npm run build` → ✓ built in 4.74s, 1869 modules transformed,
  identical bundle layout.
- `wc -l` confirms `StorageCard.tsx` shrank 1273 → 95.
- No callers outside `Dashboard.tsx` import `StorageCard`; no other module
  imported `BlastDbSection` (it was previously a private inner function), so
  the split is internally observable only.
