# New Search "Generate query" — fetch a query sequence from NCBI

## Motivation

The New Search query box only offered a fixed set of bundled FASTA examples
("Load example"). Researchers routinely start from an NCBI accession or an
organism/gene of interest, exactly the way the NCBI BLAST web form works
("Enter accession number(s), gi(s), or FASTA sequence(s)" + Query subrange).
There was no in-dashboard way to turn an accession/organism into a query
sequence, so a researcher had to leave the browser, fetch the FASTA elsewhere,
and paste it back.

## Researcher-workflow research (why accession-first, not gene-symbol)

Probing NCBI directly showed that a free-text "gene name → coordinates" mapping
is unreliable for the viral records this dashboard targets:

- `F3L` (vaccinia-style symbol) returns **0** hits in the Gene DB.
- The ortholog symbol `OPG057` resolves, but to a *different* locus than the
  F3L query the repo already ships, and poxvirus RefSeq records label genes by
  `OPG###` only — the `F3L` label is not present in the record at all.

NCBI BLAST, Batch Entrez, and `datasets` are all **accession/identifier-first**.
So the modal mirrors that: search to find an accession, then pick a gene
feature (browsed from the record's own feature table) or a manual sub-range —
no fragile gene-symbol auto-mapping.

## User-facing change

- New **"Generate query"** button next to "Load example" in the New Search
  query section (gated on a selected database, same as the other actions).
- Opens a modal that:
  1. searches `db=nuccore` by organism/keyword/accession and lists candidate
     records (accession, length, RefSeq flag, organism/title);
  2. lets the researcher enter/select an accession and **Load genes** to browse
     the record's gene/CDS features (name, product, locus_tag, coordinates,
     strand) with a filter box;
  3. accepts a manual sub-range (From/To) + strand toggle, previews the FASTA
     header (`:cSTOP-START` for the minus strand), and inserts the fetched
     FASTA into the query box (switching the program to `blastn` and refreshing
     an untouched auto title).
- All NCBI traffic is proxied through the api sidecar; the browser never calls
  NCBI directly.

## NCBI API key (Settings plumbing, key-ready)

Per the request to "wire it so a key saved in Settings later is used":

- `ncbi_identity_params()` now resolves the API key via `_resolve_api_key()`:
  the deploy-time `NCBI_API_KEY` env wins; when unset it falls back to a
  Settings store (`api/services/ncbi_pref.py`, single deployment row, masked
  reads only — the plaintext key is never returned to the browser).
- `_rate_capacity()` also consults `_resolve_api_key()`, so a key saved in
  Settings genuinely lifts the shared token bucket from 3 → 10 req/s (not just
  the `api_key` URL param). Surfaced by the self-critique pass — without it a
  Settings-only key would have passed `api_key` to NCBI yet stayed
  self-throttled at 3 req/s.
- New auth-gated routes `GET`/`PUT /api/settings/ncbi` persist/clear and read
  the masked status, with typed clients `settingsApi.getNcbiKey` /
  `putNcbiKey`. The Settings panel input UI is intentionally deferred; the
  end-to-end "saved key is honoured" path is complete and unit-tested.

## API / IaC diff summary

- New backend service `api/services/ncbi/search.py` — `search_nuccore`
  (esearch + esummary) and `fetch_feature_table` (rettype=ft parse, gene/CDS
  product merge, plus/minus strand, coordinate normalisation, byte + feature
  caps).
- New routes in `api/routes/ncbi.py`: `GET /api/ncbi/search`,
  `GET /api/ncbi/nuccore/{accession}/features` (per-caller quota + shared NCBI
  rate bucket reused; FASTA fetch reuses the existing
  `/api/ncbi/nuccore/{accession}/fasta` with `seq_start > seq_stop` for the
  minus strand — no fasta-route change).
- New `api/services/ncbi_pref.py` + `api/routes/settings/ncbi.py` (registered
  in the settings aggregator).
- `api/services/ncbi/_eutils.py`: `ncbi_identity_params` gains the env→store
  key resolver (backward-compatible; signature unchanged).
- Frontend: `web/src/api/ncbi.ts` (`searchNuccore`, `getNuccoreFeatures` +
  types), `web/src/api/settings.ts` (`getNcbiKey`/`putNcbiKey` + `NcbiKeyStatus`),
  `web/src/pages/blastSubmit/SequenceBuilderDialog.tsx` (modal + extracted pure
  helpers), `web/src/pages/blastSubmit/QuerySection.tsx` (button + insert
  handler + modal wiring).
- No IaC change.

## Validation evidence

- Backend: `uv run ruff check api` clean; `uv run pytest -q` on
  `test_ncbi_search.py` (new), `test_ncbi_pref.py` (new), `test_ncbi_nuccore.py`,
  `test_route_contracts.py`, `test_persona_matrix.py`, `test_settings_service_bus.py`
  → 169 passed.
- The new `test_ncbi_search.py` re-verifies the F3L feature span: a minus-strand
  gene at `46483..46022` parses to a 462 bp feature, matching the checked-in
  `MPXV_F3L.fa` query.
- Frontend: `npm run build` succeeds; `eslint` clean on the new/changed files;
  `vitest run src/pages/blastSubmit` → 212 passed, including
  `SequenceBuilderDialog.test.ts` (9 tests: minus-strand coordinate swap +
  `:cSTOP-START` header).
- Live NCBI cross-check (manual, during design): re-fetching
  `NC_063383.1` `seq_start=46022 seq_stop=46483 strand=2` reproduced the
  checked-in `MPXV_F3L.fa` byte-for-byte.

## Follow-up fix — large feature tables (same day)

First live use surfaced a `HTTP 422` when "Load genes" was run against a
bacterial genome: the feature-table byte cap was 1 MiB but a ~5k-gene genome's
table is ~2 MB (e.g. `AP019314.1` = 2.08 MB). Fixes:

- Raised `MAX_FEATURE_TABLE_BYTES` 1 → 6 MiB (covers bacterial/viral/organelle
  records); a chromosome-scale record that still exceeds it returns a friendly
  `ncbi_features_too_many` 422 ("Enter a sub-range manually instead.") rather
  than the raw "response too large".
- The modal now surfaces the backend error `message` (api/client.ts only fills
  `.body`, not `.error`, so the toast had shown the generic "HTTP 422").
- Live re-verified: Load genes on `AP019314.1` now renders the gene list
  (product + strand + coordinates) with zero error toasts.

