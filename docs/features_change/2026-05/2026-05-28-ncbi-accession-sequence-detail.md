# NCBI accession workflow — sequence detail page + BLAST submit-by-accession

**Motivation.** Researchers reach the dashboard from many entry points:
copy-pasting an accession from a paper, clicking a hit in BLAST results, or
exploring a feature on a GenBank record. Until now every one of those paths
ended at an external NCBI tab, which broke flow and made it easy to lose the
hit context (subject range, organism, gene/product). This change keeps the
common "inspect a sequence and re-run BLAST" loop entirely inside the dashboard
while still offering a one-click external escape.

## User-facing changes

1. **Submit BLAST by accession.** The Submit page now has an "Or fetch by
   NCBI accession" input next to the FASTA textarea. When filled (and FASTA
   empty), the backend resolves the accession via E-utilities at submit time
   and stages the FASTA exactly like an inline upload. `query_from` /
   `query_to` map to the efetch subrange instead of `-query_loc`.
2. **Sequence Detail page (`/sequence/:accession`).** New read-only viewer
   that surfaces: title, organism, taxid, length, molecule type, topology,
   updated date (esummary + GenBank), features table (with per-row
   "BLAST range" button that hands off to Submit), FASTA preview (first 8 KB,
   truncation noted), and an opt-in "Advanced view" iframe embed of the NCBI
   Sequence Viewer (sviewer) with hit-range marker.
3. **BLAST hits table → internal link.** The accession column now navigates
   to `/sequence/:accession?hl_start&hl_stop` for in-app inspection, plus a
   small external-link icon for "Open in NCBI nuccore" as a secondary action.
   The previous link had a latent bug: it stripped the `.version` segment
   from the URL (`NM_000546.6` → `NM_000546`); this also fixes that.

## Backend

* New `api/services/ncbi/` package: `_eutils` (shared HTTP plumbing,
  token-bucket rate limiter honouring NCBI 3/s no-key, 10/s with key),
  `nuccore` (esummary/efetch JSON/XML/FASTA fetch + DefusedET parsing +
  24h TTL cache), and a thin `__init__` re-export facade.
* New `api/routes/ncbi.py`: `GET /api/ncbi/nuccore/{accession}` (summary),
  `GET /api/ncbi/nuccore/{accession}/genbank`, and
  `GET /api/ncbi/nuccore/{accession}/fasta?seq_start&seq_stop`. All require
  the MSAL bearer (or `AUTH_DEV_BYPASS=true`), return 422
  `ncbi_accession_invalid` for bad input and 503 `ncbi_lookup_unavailable`
  (retryable=True, retry_after_seconds=30) for upstream outages.
* New `api/services/blast/accession_resolver.py`: bridges the new NCBI
  service into the BLAST submit pipeline without changing the Pydantic
  model (`BlastSubmitRequest.query_file` stays required — the accession
  branch populates `query_data` before validation so the existing upload
  path handles staging).
* `api/services/blast/submit_payload.py::_normalise_blast_submit_body`
  picks up `query_accession` + optional subrange, calls the resolver, drops
  the accession fields from the outgoing payload, and merges
  `{query_source: "ncbi_accession", query_accession, …}` into
  `query_metadata` after the existing query-length/count fields are filled
  in. `query_data` / `query_file` / `query_blob_url` take precedence so
  manual submits are unchanged.
* Router registration: `api/main.py` includes the NCBI router above the
  `frontend_proxy` catch-all (mandatory ordering).

## Frontend

* `web/src/api/ncbi.ts`: typed client for the three new routes.
* `web/src/pages/sequence/SequenceDetail.tsx`: new lazy-loaded page wired in
  `App.tsx` at `/sequence/:accession`. Uses TanStack Query with a 5 min
  `staleTime` so the back/forward dance from BLAST results does not re-hit
  E-utilities.
* `web/src/api/blast.ts::BlastSubmitRequest` gains optional
  `query_accession` / `query_accession_seq_start` / `query_accession_seq_stop`.
* `web/src/pages/blastSubmit/QuerySection.tsx` exposes a small accession
  input row above the FASTA textarea; tooltip explains FASTA-takes-precedence.
* `web/src/pages/blastSubmit/useSubmitMutation.ts::buildSubmitRequest`
  forwards `query_accession` only when no inline FASTA is present, and
  drops `query_from/to` from `buildEffectiveAdditionalOptions` in that
  mode so `-query_loc` does not get appended on top of the efetch subrange.
* `web/src/pages/BlastSubmit.tsx` consumes a one-shot URL handoff
  (`?accession=&from=&to=`) sent by SequenceDetail and the BLAST hits table,
  prefills the form, and strips the params from the URL.
* `web/src/pages/blastResults/analytics/BlastHitsTable.tsx` swaps the
  external `<a>` for a React Router `<Link>` to `/sequence/:accession` plus
  a small external-link icon. The Link carries `hl_start/hl_stop` derived
  from `hit.sstart/hit.send`.
* `web/src/pages/blastResults/analytics/helpers.ts`: new
  `extractCanonicalAccession()` helper; `ncbiNuccoreUrl()` now preserves the
  `.version` segment (bugfix); new `internalSequenceRoute()` helper for
  future call-sites.
* `web/nginx.conf` CSP adds `frame-src https://www.ncbi.nlm.nih.gov` to
  permit the optional sviewer embed.

## API / IaC diff summary

* Three new HTTP routes (`/api/ncbi/nuccore/{acc}` + `genbank` + `fasta`).
* No Bicep / infra change.
* No new external endpoint contacted from the browser — sviewer is the only
  cross-origin frame and it is opt-in (off by default).

## Validation evidence

* Backend: `uv run pytest -q api/tests/test_ncbi_nuccore.py` → 38/38 pass.
* Backend: `uv run pytest -q api/tests/test_blast_submit_accession.py` →
  11/11 pass.
* Backend wide sweep: `uv run pytest -q api/tests` → 1721/1721 pass,
  3 skipped (pre-existing parity skips). One earlier run flaked on
  `test_prepare_db_aks_route.py` (4 tests) — pre-existing dirty work, not
  this session; re-ran cleanly with no code change.
* Backend lint: `uv run ruff check api` → All checks passed.
* Frontend: `cd web && npm test -- --run` → 394/394 pass.
* Frontend build: `cd web && npm run build` → success; new
  `SequenceDetail-*.js` lazy chunk ≈ 7.4 KB.
* `git status --short` audit confirms only the expected files are touched;
  the unrelated dirty paths (`settings/vnet_peering`, `peering*`,
  `prepare_db*`, `SettingsPanel.tsx`, `tsconfig.tsbuildinfo`) are from a
  prior in-progress session, not this change.

## Out of scope (deliberately deferred)

* No SAS tokens / direct browser → Storage paths (charter §9 stays intact).
* No managed database for NCBI cache — the TTL cache is in-process per
  worker, deliberately small (512 entries) so a restart is the worst-case
  miss penalty.
* No bulk accession submit yet — the textarea is the path for that today;
  accession mode is single-sequence on purpose.
