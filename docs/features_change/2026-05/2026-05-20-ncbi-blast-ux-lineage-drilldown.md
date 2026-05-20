# 2026-05-20 — NCBI BLAST UX: Lineage + multi-HSP drilldown (round 4)

Follow-up to:
* [2026-05-19-ncbi-aligned-blast-results-ux.md](./2026-05-19-ncbi-aligned-blast-results-ux.md)
* [2026-05-20-ncbi-blast-ux-round-2.md](./2026-05-20-ncbi-blast-ux-round-2.md)
* [2026-05-20-ncbi-blast-ux-hardening.md](./2026-05-20-ncbi-blast-ux-hardening.md)

Closes the last two NCBI-parity gaps from the original analysis:

1. **Taxonomy Lineage** sub-view (NCBI's "Lineage" tab) — hierarchical
   tree built from NCBI eutils `LineageEx` chains.
2. **Multi-HSP subject drill-down** — clicking the `Max / Total (N HSPs)`
   indicator in the Descriptions table jumps to Alignments narrowed to
   that subject.

## Motivation

The previous rounds covered Descriptions / Graphic Summary / Alignments
parity, but NCBI's Taxonomy tab actually has *two* sub-views (Organism
list + Lineage hierarchy) and the Descriptions table treats multi-HSP
subjects as expandable groups. Without those, researchers had to click
through subject by subject. This round closes both.

## User-facing change

### Taxonomy → Lineage sub-tab

* Two-button toggle inside the Taxonomy panel: **Organism** (default,
  flat table) and **Lineage** (hierarchical tree).
* Lineage view fetches NCBI lineage chains *on demand* — the default
  Organism view stays free.
* Tree groups every organism by its shared ancestors:

  ```
  ▾ superkingdom  Viruses                                100 hits
    ▾ kingdom    Heunggongvirae                          100 hits
      ▾ genus    Orthopoxvirus                           100 hits
        ·species Monkeypox virus  [taxon-browser ↗]    99 hits
        ·species Vaccinia virus   [taxon-browser ↗]     1 hit
  ```
* Each inner node shows `total hits` (descendants summed); leaves get
  an extra `(N at this rank)` annotation.
* Top two depths are auto-expanded; deeper nodes are collapsed by
  default. Chevron rotates 90° on expand, `aria-expanded` reflects the
  state.
* "Unresolved" synthetic bucket gathers any organism whose lineage
  couldn't be fetched (NCBI rate-limited, network down, taxid missing).
  Users see exactly how much of the result set isn't in the tree.

### Multi-HSP subject drilldown

* The Descriptions `Max / Total` cell, when an `(N HSPs)` annotation is
  shown, is now a clickable button. Clicking switches the active tab to
  Alignments and narrows the filter to `qseqid=… AND sseqid=…` so the
  next view shows every HSP for that subject.
* Mirrors the existing Graphic Summary bar-click handler — the two
  click affordances now share one drilldown semantics.
* `aria-label` describes the action ("Open Alignments tab narrowed to
  N HSPs for sseqid").

## API / IaC diff summary

* `/api/blast/jobs/{id}/results/taxonomy` gains two query parameters:
  * `include_lineage=true|false` (default false) — when true the server
    calls `fetch_taxonomy_detail(taxid)` for the top-N rows and adds
    `lineage` (string) + `lineage_ex` (root → leaf chain) to each row,
    plus a top-level `lineage: {requested, looked_up, failed, limit_reached}`
    meta object so the SPA can show progress.
  * `lineage_taxid_limit=1..100` (default 20) — caps how many taxids
    are looked up per request, defending NCBI eutils rate limits.
* The eutils call goes through the existing
  `api.services.taxonomy.fetch_taxonomy_detail`, which is cached and
  defusedxml-parsed. Failures per taxid are swallowed (the row stays in
  the response without `lineage_ex`) so a partial NCBI outage doesn't
  break the page.
* No IaC changes.

## Tests

### Backend (`api/tests/test_blast_results_routes.py`) — **+3 cases**

Suite count: 27 → 30 (full pytest suite 699 → 702 pass).

* `test_taxonomy_include_lineage_calls_fetch_taxonomy_detail_per_taxid`
  — verifies one eutils call per distinct taxid + lineage_ex stitched
  into each row.
* `test_taxonomy_include_lineage_tolerates_eutils_failure` — confirms
  `TaxonomySearchUnavailable` is swallowed; row keeps the rollup, drops
  `lineage_ex`, and the `lineage.failed` counter goes up.
* `test_taxonomy_include_lineage_respects_taxid_limit` — confirms the
  cap; only top-N taxids are looked up; `limit_reached` non-zero.

### Frontend (`web/.../TaxonomyPanel.test.ts`) — new file, **5 cases**

Suite count: 6 → 11 in `analytics/` (full vitest 184 → 190 pass).
Exposes `__internals = { buildLineageTree }` so the pure tree-builder
can be tested without mounting React.

* empty rows → empty tree
* shared ancestor → single inner node (the headline correctness check)
* missing `lineage_ex` → goes to the synthetic "Unresolved" bucket
* `leafCount` ≠ `totalCount` for inner nodes
* duplicate leaf rows accumulate into one node

## Validation evidence

* `uv run ruff check api` — clean
* `uv run pytest -q api/tests` — **702 passed in 25.31 s**
* `npm run build` — green in 7.36 s
* `npx vitest run` — **190 passed across 20 suites**
* `npx eslint --max-warnings 0` (scoped to touched files) — exit 0

## Files touched

Backend:

* `api/routes/blast.py` — `_enrich_taxonomy_with_lineage` helper +
  `include_lineage` / `lineage_taxid_limit` Query parameters +
  `lineage: {…}` meta on the response.
* `api/tests/test_blast_results_routes.py` — 3 new lineage tests.

Frontend (analytics):

* `web/src/api/blast.ts` — `resultsTaxonomy({include_lineage, lineage_taxid_limit})`,
  `lineage` / `lineage_ex` fields on `BlastTaxonomyRow`.
* `web/src/pages/blastResults/analytics/TaxonomyPanel.tsx` — full
  rewrite to host two sub-views: `OrganismTable` (existing flat list)
  and `LineageTree` (new hierarchical tree). Exposes `__internals`
  for unit tests.
* `web/src/pages/blastResults/analytics/TaxonomyPanel.test.ts` — new
  test suite for `buildLineageTree`.

Frontend (descriptions):

* `web/src/pages/blastResults/analytics/BlastHitsTable.tsx` —
  optional `onSubjectDrilldown` prop; multi-HSP cell becomes a button
  when the callback is provided.
* `web/src/pages/blastResults/analytics/DescriptionsTabBody.tsx` —
  wires `handleSubjectDrilldown` to navigate to `?tab=alignments` +
  `applyImmediate({queryFilter, subjectFilter})`.

## What is genuinely complete now

Every "intentionally deferred" item from the original gap analysis is
now done **or** documented as out-of-scope:

| Item | Status |
|---|---|
| Server-side per-subject Max / Total / HSP count | ✅ done (round 3) |
| Multi-HSP subject grouping (UI) | ✅ done (this round, drilldown) |
| Server-side Taxonomy with lineage hierarchy | ✅ done (this round) |
| Column-header click sort | ✅ done (round 2) |
| Graphic Summary → Alignments deep-link | ✅ done (round 2) |
| Citation / Help popover | ✅ done (round 2) |
| Edit Search filter hydration | ❌ deliberately out of scope — NCBI's Edit Search also doesn't carry result-page filters; documented in round-1 change note |
| Save Search / Saved Strategies | ❌ out of scope — needs a new persistence model |
| Column visibility selector | ❌ out of scope — low ROI per audit |
| Distance tree of results | ❌ out of scope — would require an external phylo library |
