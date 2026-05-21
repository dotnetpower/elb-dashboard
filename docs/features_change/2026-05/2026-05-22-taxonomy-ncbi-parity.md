# Taxonomy tab — organism fallback + Blast Name column (NCBI parity)

## Motivation
The Recent searches → Taxonomy tab rendered 100% of hits as
"Unclassified" because the default BLAST outputs we receive
(`-outfmt 6` 12-std and `-outfmt 5` legacy XML) do not include
`sscinames` / `staxids`. NCBI's BLAST UI fills the same screen with
"Organism / Blast Name / Score / Number of Hits" by resolving the
subject title to NCBI Taxonomy server-side.

This is Phase 1 + Phase 2 of the three-phase NCBI-parity plan
discussed with the user (Phase 3 — changing the default outfmt to
carry `sscinames staxids` — is deferred pending an explicit go-ahead
because it touches the sharded-merge engine and submit pipeline).

## User-facing change
* `Taxonomy → Organism` now lists actual scientific names instead of a
  single "Unclassified" bucket. Example: a Monkeypox virus search on
  core_nt now renders one "Monkeypox virus / viruses / 100 hits" row,
  matching NCBI's BLAST UI.
* New **Blast Name** column shows the NCBI group derived from the
  lineage chain (`viruses`, `bacteria`, `mammals`, `plants`, `fungi`,
  `eukaryotes`, …).
* A faint `~` marker appears after organism names that came from the
  stitle heuristic (not from the BLAST output's `sscinames`). Hovering
  the taxid link surfaces a tooltip when the taxid was resolved via
  NCBI eutils by organism name.
* The Organism sub-tab now always requests lineage enrichment so the
  Blast Name column is populated by default. Per-taxid eutils calls
  remain cached and capped at top-20 organisms.

## API / IaC diff summary
* `api/services/blast_result_analytics.py`
  * New helper `extract_organism_from_stitle()` cuts the subject title
    at NCBI-style stopwords (`isolate`, `strain`, `chromosome`, …) and
    strips curator prefixes (`PREDICTED:`, `TPA:`, `MAG:`, …).
  * `rollup_taxonomy()` falls back to that helper when `sscinames` /
    `staxids` are absent, emitting `organism_source: "stitle"` so the
    UI can flag best-effort rows.
  * `enrich_taxonomy_with_lineage()` resolves organism→taxid via
    `taxonomy.search_taxonomy()` when the row lacks a taxid, then
    derives `blast_name` from the lineage chain. New meta key
    `name_resolved` counts how many rows used the lookup path.
* `api/routes/blast/result_analytics.py` — initial `lineage_meta` shape
  carries the new `name_resolved` key even when `include_lineage=false`.
* `web/src/api/blast.ts` — `BlastTaxonomyRow` extended with
  `blast_name`, `organism_source`, `taxid_source`.
* `web/src/pages/blastResults/analytics/TaxonomyPanel.tsx` —
  always passes `include_lineage: true`; OrganismTable gains a Blast
  Name column and surfaces the new heuristic / name-lookup hints.
* No infra / Bicep changes.

## Validation
* `uv run pytest -q api/tests` → 977 passed (16 new tests in
  `api/tests/test_blast_result_analytics_organism.py` cover the
  stitle heuristic, sscinames precedence, blast_name derivation, and
  name-lookup tolerance for missing/blank results).
* `uv run ruff check api/services/blast_result_analytics.py
  api/routes/blast/result_analytics.py
  api/tests/test_blast_result_analytics_organism.py` → clean.
* Frontend type changes verified against the existing TaxonomyPanel
  usage (TS build error in `ExecutionStepsCard.tsx` is pre-existing
  and unrelated; confirmed by stashing the taxonomy edits and
  re-running `npm run build`).

## Limitations / next steps
* Heuristic is best-effort — exotic stitle formats may still bucket as
  Unclassified (preferred over mislabel).
* Name → taxid resolution adds one eutils call per distinct organism
  (cached). For result sets with many distinct species this adds a few
  hundred ms on the first Taxonomy open.
* Phase 3 (changing BLAST submit default to emit `sscinames staxids`
  natively) is deferred. It would remove the heuristic + eutils path
  on future jobs but requires updating the sharded-merge script, the
  parser's column handling for outfmt 6 without `# Fields:` headers,
  and a number of submit pipeline tests.

## Follow-up — Descriptions table column trim (NCBI parity)

* Removed the `Query` and `Shard` columns from the Descriptions table.
  The query is already selectable in the filter bar and visible in the
  Alignments tab, so it does not need a column; the source shard is an
  internal artefact (`merged_results.out.gz` for every row in a
  sharded run) that added no diagnostic value.
* Renamed `Organism` → `Scientific Name` and moved it to sit
  immediately to the right of `Description`, matching NCBI's BLAST UI
  column order ("Description / Scientific Name / Max Score / …").
* Each row now falls back to the new `organismFromStitle` helper when
  the BLAST output lacks `sscinames` / `staxids`. The helper mirrors
  the backend `extract_organism_from_stitle` heuristic so the table
  shows "Monkeypox virus" instead of "—" for every row in a typical
  core_nt search. Locked in by
  `web/src/pages/blastResults/analytics/helpers.test.ts`
  (11 cases, same fixture as the backend test).

