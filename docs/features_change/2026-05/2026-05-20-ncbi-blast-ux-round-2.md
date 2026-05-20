# 2026-05-20 — NCBI-aligned BLAST results UX (round 2)

Follow-up to [2026-05-19-ncbi-aligned-blast-results-ux.md](./2026-05-19-ncbi-aligned-blast-results-ux.md).
Picks up the four items that the previous round explicitly deferred.

## Motivation

The first round restructured the page IA and named everything in BLAST
domain terms; researchers transitioning from NCBI Web BLAST asked for the
four remaining interactions that close the "muscle memory" gap:

1. Sort by clicking the column header (NCBI sorts immediately, not via a Apply button).
2. Click a Graphic Summary hit bar to jump straight into that hit's alignment.
3. A discoverable Help / Citation menu (NCBI puts BLAST citation links one click away — vital for papers).
4. The `Max / Total bit score (N HSPs)` triple that NCBI shows when a subject has multiple HSPs.

## User-facing change

* **Column-header click sort** — Descriptions table headers for HSP
  Cover, % Identity, Length, E-value, and `Max / Total` are now
  clickable with sort-direction chevrons. Click toggles direction;
  click a different column to switch. Sort is applied *immediately*
  (bypasses the filter Apply button) because NCBI does the same and
  the indirection felt wrong. The sort dropdown in the filter bar
  still works and stays in sync.
* **Graphic Summary → Alignments deep-link** — Hit bars are now
  buttons (`role=button`, keyboard activatable). Clicking one sets
  the active tab to Alignments and narrows the filter to that query
  + subject so the per-hit pairwise card appears immediately. Tooltip
  updated to "Click to open in the Alignments tab".
* **Help / Citation pop-out** — New `BlastHelpMenu` lives next to the
  "Recent searches" back link. Contains:
  * "How to read this report" links (NCBI Bookshelf + Handbook + YouTube playlist)
  * Canonical BLAST citations (Altschul 1990, Altschul 1997, Camacho 2009; Zhang 2000 added for blastn)
  * Reminder to cite BLAST+ for ElasticBLAST-driven results.
  * Outside-click + Esc closes the menu.
* **Max / Total bit score column** — The previous "Bit Score" column
  is now "Max / Total". When a subject has more than one HSP on the
  current page, the cell renders `854 / 1708 (2 HSPs)`; single-HSP
  rows still show just `854`. The total + count are computed on the
  frontend from the alignments page; no backend change needed.

## API / IaC diff summary

None — frontend-only. The subject aggregate is a `Map<sseqid, …>` built
in `useSubjectAggregates` from the alignments query, so the page-size
cap on the existing `/results/alignments` endpoint already bounds the
cost. If multi-HSP grouping needs to span the full result set later,
the right move is a server-side rollup on
`/api/blast/jobs/{id}/results/alignments` rather than enlarging the
client computation.

## Files touched

* `web/src/pages/blastResults/analytics/useBlastAnalyticsState.ts`
  — added `applyImmediate(patch)` to skip the pending/Apply roundtrip
  for click-driven actions.
* `web/src/pages/blastResults/analytics/BlastHitsTable.tsx`
  — `SortableHeader` component, click-to-sort wiring, per-subject
  aggregate hook + `Max / Total` cell.
* `web/src/pages/blastResults/analytics/GraphicSummaryPanel.tsx`
  — `HitBar` is now a button; `useNavigate` + `applyImmediate`
  swap the active tab and narrow the filter.
* `web/src/pages/blastResults/BlastHelpMenu.tsx` (new) — pop-out menu.
* `web/src/pages/blastResults/BlastJobHeader.tsx` — anchors the
  Help menu to the top-right of the back-link row.

## Validation evidence

* `npm run build` — green (7.24 s).
* `npx eslint --max-warnings 0` scoped to touched files — exit 0.
* No backend changes; no pytest re-run necessary beyond the prior
  670-pass baseline.

## Still intentionally deferred

* Server-side per-subject `Max / Total / HSP count` so the rollup
  spans the whole result set (current rollup is page-local).
* Server-side Taxonomy lineage tree.
* Edit Search button persisting filter state into the New BLAST form.
