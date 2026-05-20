# 2026-05-20 — NCBI-aligned BLAST UX: hardening + server-side rollups

Follow-up to:
* [2026-05-19-ncbi-aligned-blast-results-ux.md](./2026-05-19-ncbi-aligned-blast-results-ux.md)
* [2026-05-20-ncbi-blast-ux-round-2.md](./2026-05-20-ncbi-blast-ux-round-2.md)

Closes the remaining "Intentionally deferred" items and applies a
self-audit pass on the rounds-1+2 frontend.

## Motivation

The first two rounds shipped the IA + tabs + interactions but explicitly
deferred (a) the server-side rollups so the Descriptions table's
`Max / Total` cell and the Taxonomy tab span the whole result set, not
just the visible page, and (b) a handful of accessibility / race
hardening items uncovered by a follow-up audit. This change does both.

## User-facing change

### Server-side rollups (replaces the page-local fallback)

* **Descriptions `Max / Total` cell**: `/api/blast/jobs/{id}/results/alignments`
  now returns a `subject_aggregates: SubjectAggregate[]` field —
  one row per `sseqid` with `{max_bitscore, total_bitscore, hsp_count,
  stitle, sscinames, staxids}`, computed across the *filtered* hit set
  (not the visible page). Capped at 5 000 rows. The SPA prefers the
  backend rollup; if absent (older API, degraded payload) it falls
  back to the page-local `buildSubjectAggregates`.
* **Taxonomy tab** is now powered by a dedicated endpoint:
  `/api/blast/jobs/{id}/results/taxonomy?…` accepts the same filter
  parameters as `/results/alignments` so a narrowing on Descriptions
  carries through. Returns `{organisms, total_hits, filtered_hits,
  files_parsed, total_files, read_failures, …}`. Rolls up by
  `sscinames` (falling back to `staxids`, then `"unclassified"`).
  Sorted by hit count desc, capped at 2 000 organisms. Frontend
  caption distinguishes "(full result set)" vs "(visible page only)"
  so the researcher knows which rollup they're looking at.

### Round-1 hardening (self-audit follow-ups)

The audit list, with each item resolved:

| # | Issue | Fix |
|---|---|---|
| B1 | `DownloadAllMenu` had no outside-click handler | added `useEffect`/`useRef`-based outside-click + Esc, mirrors `BlastHelpMenu` |
| B2 | `SortableHeader` `<th onClick>` was keyboard-inaccessible | `role="button"` + `tabIndex=0` + Enter/Space handler + `aria-sort` + `aria-label` |
| B3 | `applyImmediate` called `setApplied` inside `setPending` updater (strict-mode warning risk) | rewritten as two parallel pure functional updaters; preserves unsaved pending edits |
| B4 | external links used `rel="noreferrer"` only | upgraded to `rel="noopener noreferrer"` everywhere (4 sites) |
| B5 | `\n` in `<HitBar>` title rendered as a literal space | replaced with `·` separator |
| B6 | range filter min > max was silently sent to the backend | inline `role="alert"` pre-flight banner + Apply button disabled |
| B7 | `useSubjectAggregates` mis-named (no hook calls inside) | renamed to `buildSubjectAggregates` (pure helper) + `useMemo` at call site |
| A1 | bulk action buttons missing `aria-label` | added on Download / Send to MSA / per-row checkbox |
| A2 | `DownloadAllMenu` lacked `aria-haspopup` / `aria-expanded` / `role="menuitem"` | added all three |

### Terminology pass — leftovers

The earlier round renamed nav `Jobs → Recent searches` and most page
strings, but the visual smoke caught three more places:

* Breadcrumb `jobs` label `Jobs → Recent searches`
* `JobsEmptyState` filter copy `No jobs matching "x" → No searches matching "x"`
* `LatestJobChip` empty-state pill `No jobs → No searches`, tooltip aligned

## API / IaC diff summary

* New `/api/blast/jobs/{id}/results/taxonomy` — same auth/quota/storage
  envelope as the sibling `/results/alignments`; returns the per-organism
  rollup. No new permissions.
* `/api/blast/jobs/{id}/results/alignments` response gains
  `subject_aggregates`. Existing callers ignore unknown fields, so the
  addition is backwards-compatible.
* No IaC changes — both endpoints live in the existing `api` sidecar
  and use the same MI-bound storage credential.

## Tests

* `api/tests/test_blast_results_routes.py` — **+6 cases**
  (21 → 27 in this file; full suite 670 → 699 pass, all green):
  * `test_alignments_returns_subject_aggregates_with_max_total_and_hsp_count`
  * `test_alignments_subject_aggregates_respect_filters`
  * `test_taxonomy_returns_per_organism_rollup_with_filters`
  * `test_taxonomy_returns_empty_when_no_blobs`
  * `test_taxonomy_handles_unclassified_when_metadata_missing`
  * `test_taxonomy_degraded_when_all_reads_fail`
* `web/.../BlastHitsTable.test.ts` — **new file, 6 cases** for
  `buildSubjectAggregates` (empty, single-HSP, multi-HSP, string
  bitscore, NaN bitscore, empty-sseqid bucket).
* Hook-level tests for `applyImmediate` were drafted but **not
  shipped**: `@testing-library/react` is not in `package.json` and
  the charter rule is "no new dependency without justification". The
  hook's behaviour is exercised end-to-end by the existing browser
  smoke flow.

## Validation evidence

* `uv run ruff check api` — clean
* `uv run pytest -q api/tests` — **699 passed in 28.25 s**
* `npx vitest run src/pages/blastResults/analytics` — 6 / 6 pass
* `npm run build` — 9.67 s, green
* `npx eslint --max-warnings 0` (scoped to every file I touched) — exit 0
* Visual smoke at `http://127.0.0.1:8090/blast/jobs` — header reads
  "Recent BLAST searches", nav shows "Recent searches", empty state
  reads "No BLAST searches yet."

## Files touched

Backend:

* `api/routes/blast.py` — `_rollup_subject_aggregates`,
  `_rollup_taxonomy` helpers + new `/results/taxonomy` route +
  `subject_aggregates` field on `/results/alignments`.

Frontend (analytics):

* `web/src/api/blast.ts` — new `resultsTaxonomy(...)` client,
  `BlastSubjectAggregate` + `BlastTaxonomyRow` types,
  `subject_aggregates` field on the alignments response.
* `web/src/pages/blastResults/analytics/useBlastAnalyticsState.ts` —
  `applyImmediate` rewritten as two pure functional updaters.
* `web/src/pages/blastResults/analytics/BlastHitsTable.tsx` —
  `SortableHeader` keyboard a11y, ARIA labels, `useMemo` over
  `buildSubjectAggregates` (renamed + exported), backend-aggregate
  preference with frontend fallback.
* `web/src/pages/blastResults/analytics/TaxonomyPanel.tsx` —
  rewritten to call the new `/results/taxonomy` endpoint with a
  page-local fallback; caption tells the researcher which they
  are looking at.
* `web/src/pages/blastResults/analytics/ResultFilterBar.tsx` —
  range pre-flight validation + inline error.
* `web/src/pages/blastResults/analytics/GraphicSummaryPanel.tsx` —
  tooltip newline → `·` (browser title-attribute compat).
* `web/src/pages/blastResults/BlastJobHeader.tsx` —
  `DownloadAllMenu` outside-click / Esc / ARIA.
* `web/src/pages/BlastResults.tsx` — pass storage props through to
  `<TaxonomyPanel>`.

Frontend (terminology leftovers):

* `web/src/components/Breadcrumb.tsx` — `jobs` label.
* `web/src/components/LatestJobChip.tsx` — empty pill + tooltip.
* `web/src/pages/BlastJobs/JobsEmptyState.tsx` — filter-empty copy.

Tests:

* `api/tests/test_blast_results_routes.py` — 6 new cases.
* `web/src/pages/blastResults/analytics/BlastHitsTable.test.ts` — new file.
