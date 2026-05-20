# 2026-05-19 — NCBI-aligned BLAST results UX

## Motivation

Researchers transitioning from NCBI Web BLAST to ElasticBLAST kept asking
"where are the hits?" because the result entry-point was a file-list page,
not an alignments table. The page also used software-engineering vocabulary
("Job", "Duplicate", "Analytics") instead of the BLAST-domain language they
expected ("Search", "Edit search", "Descriptions"). This change ports the
NCBI Web BLAST information architecture to the elb-dashboard so a
researcher can navigate the same way they always have.

## User-facing change

* `/blast/jobs/:jobId` is now a single tabbed page in NCBI's tab order:

  | Tab | Equivalent on NCBI | New in this PR |
  |---|---|---|
  | Descriptions | "Sequences producing significant alignments" | only the page restructure — table itself existed under /analytics |
  | Graphic Summary | identical | YES — new component, query ruler with NCBI score-binned bars |
  | Alignments | identical | only the page restructure |
  | Taxonomy | "Reports → Organism" | YES — frontend rollup of `sscinames` / `staxids` |
  | Files | (ElasticBLAST-only) | moved here from the old root-level Results card |
  | Run details | (ElasticBLAST-only) | moved here from the old root-level cards |

  The legacy `/blast/jobs/:id/analytics` route still resolves — it now
  redirects to `?tab=descriptions` for bookmark compatibility.

* Active tab is encoded in `?tab=...` so deep-links and the browser
  back/forward buttons keep working.

* Job header now matches NCBI's 7-line metadata grid (Search ID,
  Submitted, Program, Database, Query ID, Molecule type, Description,
  Query length, E-value cutoff, Cluster, Region). The previous header
  hid the program/database behind a sub-card.

* `Duplicate` button renamed to **Edit search** (NCBI's term),
  promoted to primary. `Export config` renamed to **Save settings**.
  A new **Download all** combo dropdown next to "Submitted" exports
  every shard's hits as CSV / TSV / JSON.

* Filter bar now offers **from-to range inputs** for Identity %,
  HSP cover, Max E. An explicit **Apply / Reset** pair plus an
  **active-filter chip row** mirrors NCBI's "Filter Results" panel —
  no more refetching on every keystroke. Page-size selector
  (10 / 50 / 100 / 250) added.

* Descriptions table gains **row checkboxes + select-all** with a
  sticky action bar (`N selected · Download selection (FASTA) · Send
  to MSA Viewer · Clear`). The MSA Viewer action opens NCBI's MSA
  Viewer with the selected accessions pre-loaded.

* Per-hit Accession links now deep-link to **NCBI nuccore**
  (`ncbi.nlm.nih.gov/nuccore/<acc>`) instead of a generic search URL.

* Graphic Summary applies the **canonical NCBI score color palette**
  (<40 black, 40-50 blue, 50-80 green, 80-200 magenta, ≥200 red) with
  the same legend NCBI uses.

* Alignments cards now show the NCBI stat line — `Identities 462/462
  (100%)`, `Gaps N/total (pct)`, **Strand: Plus/Plus** or
  **Plus/Minus** — instead of percent-only.

* Copy / terminology:
  * Nav "Jobs" → **Recent searches**
  * Page title "ElasticBLAST Jobs" → **Recent BLAST searches**
  * `Cancel Job` / `Delete BLAST Job` → `Cancel search` / `Delete BLAST search`
  * `Loading BLAST jobs…` → `Loading BLAST searches…`
  * `No hits found` → **No significant similarity found**
  * `No BLAST result files (.out) found in results/<id>/` → **No
    significant similarity found. BLAST returned no hits for this
    query/database combination.**
  * `No results available — the job failed at the X step` →
    **Search failed during the X step. Open the Run details tab for
    diagnostics.**

* Sticky tab bar at the top of the result page so tabs stay reachable
  when scrolling a long hit list.

## API / IaC diff summary

* **No backend changes.** Backend already exposes
  `/api/blast/jobs/{id}/results/aggregate`,
  `/api/blast/jobs/{id}/results/alignments`,
  `/api/blast/jobs/{id}/results/export`. The Taxonomy tab does a
  frontend rollup of `sscinames`/`staxids` returned by alignments;
  if a server-side rollup is needed later, the UI shape under
  `TaxonomyPanel.rollupByOrganism` matches what an endpoint should return.
* **No IaC changes.**

## Files touched

New components (all under `web/src/pages/blastResults/`):

* `BlastResultsTabs.tsx` — sticky tab bar + URL <-> tab mapping
* `analytics/helpers.ts` — shared formatters + NCBI score-color palette
* `analytics/useBlastAnalyticsState.ts` — queries, filter state, selection
* `analytics/DegradedBanner.tsx`
* `analytics/ResultFilterBar.tsx` — range inputs + Apply/Reset + chips
* `analytics/BlastHitsTable.tsx` — bulk select + nuccore links
* `analytics/AlignmentViewer.tsx` — strand + identity fractions
* `analytics/OverviewPanel.tsx`
* `analytics/GraphicSummaryPanel.tsx` (NEW — query ruler)
* `analytics/TaxonomyPanel.tsx` (NEW — organism rollup)
* `analytics/DescriptionsTabBody.tsx`
* `analytics/AlignmentsTabBody.tsx`

Modified:

* `web/src/pages/BlastResults.tsx` — tab orchestrator
* `web/src/pages/BlastAnalytics.tsx` — 1381-line page → 20-line redirect
* `web/src/pages/blastResults/BlastJobHeader.tsx` — NCBI 7-line + Edit search + Download all
* `web/src/pages/blastResults/BlastJobMetrics.tsx` — points to `?tab=descriptions`
* `web/src/pages/blastResults/BlastResultsTable.tsx` — empty-state copy
* `web/src/pages/blastResults/ResultsBody.tsx` — empty-state copy
* `web/src/components/Layout.tsx` — nav label "Jobs" → "Recent searches"
* `web/src/pages/BlastJobs/JobsHeader.tsx` — title "ElasticBLAST Jobs" → "Recent BLAST searches"
* `web/src/pages/BlastJobs/JobsEmptyState.tsx` — empty-state copy
* `web/src/pages/BlastJobs/BlastJobs.tsx` — confirm + loading copy

## Validation evidence

* `npm run build` — green (1 build, 6.82 s)
* `npx eslint --max-warnings 0` (scoped to touched files) — exit 0
* `uv run pytest -q api/tests` — 670 pass; 2 fails are pre-existing
  uncommitted WIP in `test_external_blast_api.py` for the not-yet-
  implemented `external_degraded` response field. No backend code in
  this change.
* Dev server (`http://127.0.0.1:8090`) — `GET /` returns 200,
  `GET /blast/jobs` returns 200.

## Intentionally deferred (not in scope)

The following items from the original UX gap analysis were skipped
because they require backend work or are P2-only polish:

* Multi-HSP subject grouping in the Descriptions table (needs
  backend grouping logic).
* Total-score column (max + sum of HSPs per subject — needs
  backend aggregate change).
* Column-header click sort (current dropdown still functional).
* Filter sidebar layout (current top bar with range + Apply
  matches the same intent — promoted to sidebar would need a
  responsive split layout).
* Server-side Taxonomy tree with full lineage (current frontend
  rollup is page-local).
* Citation popover + Help / How-to-read dropdown.
* `?tab=alignments` deep-link from a Graphic Summary hit-bar click.
