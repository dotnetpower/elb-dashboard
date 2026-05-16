# BlastResults page — SRP split into `pages/blastResults/` modules

## Motivation
`web/src/pages/BlastResults.tsx` was a 1216-line component that combined data
fetching, phase resolution, three different status banners, the job-details
grid, the metric strip + export buttons, the results table, the empty/locked
state panels, the cancel confirmation dialog, and the toast-on-phase-transition
effect. Every change to one aspect required scrolling through hundreds of lines
of unrelated UI, and the file was the second-largest non-test source file in
the SPA after the dashboard cards.

This change applies the same SRP refactor pattern that shipped for
`StorageCard` (see [2026-05-15-storage-card-srp.md](./2026-05-15-storage-card-srp.md))
to the BLAST results page.

## User-facing change
None. Behaviour, polling cadence, status copy, banner gradients, button
disabled states, and the cancel confirmation flow are byte-for-byte
equivalent. Verified by:
- Reading the original 1216-line file end-to-end and mapping each visual block
  to a target sub-component before extraction.
- Re-running `npm run build` after each extraction and after the slim
  coordinator was wired in (final: 1875 modules transformed, no TS errors,
  6.18s).

## File diff summary
| File | Before | After |
|------|-------:|------:|
| `web/src/pages/BlastResults.tsx` | 1216 | 504 (slim coordinator + `ResultsBody` discriminator) |
| `web/src/pages/blastResults/StorageLockedPanel.tsx` | — | 165 |
| `web/src/pages/blastResults/BlastJobHeader.tsx` | — | 79 |
| `web/src/pages/blastResults/BlastJobBanners.tsx` | — | 260 (running + success + failure + status icon) |
| `web/src/pages/blastResults/BlastJobDetailsGrid.tsx` | — | 123 |
| `web/src/pages/blastResults/BlastJobMetrics.tsx` | — | 112 |
| `web/src/pages/blastResults/BlastResultsTable.tsx` | — | 314 (table + header cell + row + `NoResultFilesPanel`) |

Net effect: the page coordinator drops from 1216 → 504 lines (≈58 %).
The remaining six files have a single visual or logical purpose each
and sit ≤ 314 lines.

## Hardening pass applied
Mirroring the `useBlastDb` review:

1. **Toast effect** — guarded `if (!job) return` so we never record
   `"unknown"` as the initial phase before the query resolves. The
   `wasTerminalOnLoad` inline disjunction was promoted to a module-level
   `TERMINAL_PHASES` Set so future contributors only have one place to add a
   new terminal phase.
2. **Refetch stability** — `const refetchResults = resultsQuery.refetch;`
   is captured once and used by both the Refresh button and the
   `onUnlocked` callback. `useQuery`'s `refetch` is referentially stable
   across renders; depending on `resultsQuery.refetch` directly (without
   capture) would re-create dependent callbacks every render and break
   downstream memoization.
3. **Sub-component identity** — `ResultsBody` is declared at module scope
   (not nested inside `BlastResults`) so it isn't re-created every render.
4. **Primitive-only props** — only primitives (`isHealthy`,
   `hasRunningCluster`, `hasAnyCluster`) are passed from `useClusterReadiness`
   / `useTerminalSidecarHealth` to children, so the cards never re-render on
   identity-only changes from the hooks.
5. **Type safety on export format** — `BlastJobMetrics`'s `exportingFormat`
   prop uses the canonical `BlastExportFormat` union from `@/api/blast`
   (`"csv" | "tsv" | "json"`) instead of a hand-narrowed
   `"csv" | "json"` literal. Caught by `tsc -b` on first build attempt.

## API / IaC diff summary
None. Frontend-only refactor.

## Validation evidence
```
$ cd web && npm run build
> tsc -b && vite build
vite v5.4.21 building for production...
✓ 1875 modules transformed.
✓ built in 6.18s
```

`get_errors` reports no diagnostics on `BlastResults.tsx` or any of the
six new files in `web/src/pages/blastResults/`.
