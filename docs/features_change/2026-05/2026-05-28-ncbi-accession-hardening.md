# 2026-05-28 — NCBI accession + SequenceDetail hardening (10 critique items)

## Motivation

Follow-up to `2026-05-28-ncbi-accession-sequence-detail.md`. A 10-item self-critique surfaced four classes of bugs in the just-shipped pipeline:

1. **Sticky pooled HTTP defaults** — `_pooled_client` baked `Accept` into a shared httpx slot. JSON and bytes calls reuse the same slot, so the second caller would silently inherit the first caller's `Accept` header.
2. **Half-read responses on stream overflow** — `request_bytes` raised mid-stream without closing the response, leaving httpx's connection pool in an unknown state on the next NCBI call.
3. **Cache hygiene** — TTL cache used shallow `dict(payload)` (nested feature lists shared by reference across callers) and FIFO eviction (hot accessions paged out even when freshly accessed).
4. **Validation surface gaps** — `query_accession` + `query_data` silently picked one (audit ambiguity); whole-sequence BLAST > 5 MiB landed as a 503 retryable instead of a 422 user-fixable; the dashboard rendered a broken internal `<Link>` for non-accession BLAST hits (`Query_1`, custom DB IDs); URL handoff wiped an in-progress FASTA draft without confirmation.

## User-facing change

- **BlastHitsTable** — non-accession `sseqid` values now render plain text + an external NCBI search link instead of a broken in-app SequenceDetail link.
- **SequenceDetail** — summary → GenBank → FASTA now load sequentially. A typo accession only burns one rate-limit token instead of three, and the page renders incrementally instead of all-or-nothing. Whole-sequence BLAST > 5 Mnt is gated by a `window.confirm` so researchers can switch to a sub-range first.
- **BlastSubmit** — `/blast/submit?accession=…` handoff preserves an existing inline FASTA draft and surfaces a `Kept your existing FASTA draft` toast. Submitting both still results in 422 backend-side, but the UI no longer destroys work silently.
- **Submit error codes** — accession resolution now distinguishes `ncbi_query_too_large` (422, fix-the-input) from `ncbi_lookup_unavailable` (503, retryable). Mixed query sources raise `conflicting_query_sources` (422) instead of silent precedence.
- **Sequence Viewer iframe** — sandbox flags are unchanged, but the embed now ships with an inline comment explaining why `allow-scripts allow-same-origin` is required for the NCBI viewer to render and what would have to change to drop it.

## Backend diff summary

- `api/services/ncbi/_eutils.py`:
  - `_pooled_client(slot)` now ships only `User-Agent` headers. Callers pass `Accept` per-request.
  - `request_json` / `request_bytes` pass `headers={"Accept": ...}` per call.
  - `request_bytes` short-circuits on `Content-Length` and explicitly `response.close()`s before raising on overflow.
  - New `NcbiResponseTooLarge(NcbiServiceUnavailable)` subclass for non-retryable size-cap overflows.
- `api/services/ncbi/__init__.py` re-exports `NcbiResponseTooLarge`.
- `api/services/ncbi/nuccore.py`:
  - Cache buckets switched to `OrderedDict` with `move_to_end` on hit (true LRU).
  - `_cache_get` / `_cache_put` now `copy.deepcopy` payloads in both directions; callers can mutate freely without polluting the cache.
- `api/services/blast/accession_resolver.py` maps `NcbiResponseTooLarge` to `HTTPException(422, ncbi_query_too_large)` with a sub-range hint.
- `api/services/blast/submit_payload.py` rejects `query_accession` + (`query_data` | `query_file` | `query_blob_url`) with `HTTPException(422, conflicting_query_sources)` BEFORE resolving the accession.

## Frontend diff summary

- `web/src/pages/blastResults/analytics/helpers.ts`:
  - New `ACCESSION_PATTERN` mirroring the backend `_ACCESSION_RE`.
  - New `isNcbiAccessionLike(accession)` predicate gating in-app SequenceDetail links.
- `web/src/pages/blastResults/analytics/BlastHitsTable.tsx`:
  - Subject cell branches on `isNcbiAccessionLike(hit.sseqid)`. Accession-like sseqids retain the internal `<Link>` + external icon; everything else renders plain text + an external NCBI search link.
- `web/src/pages/sequence/SequenceDetail.tsx`:
  - `genbankQuery.enabled` waits for `summaryQuery.data`; `fastaQuery.enabled` waits for `genbankQuery.data`.
  - `launchBlast` confirms before navigating when `summary.length > 5_000_000` and `!hasHighlight`, citing the backend error code in the dialog.
  - Inline comment on the sviewer `<iframe>` documents why `allow-scripts allow-same-origin` is required and what would have to change to revisit.
- `web/src/pages/BlastSubmit.tsx`:
  - URL handoff useEffect now reads `form.query_data` from mount state and skips the accession overwrite when an inline FASTA draft is present. Both branches emit distinct toasts.

## API / IaC diff

- New error code in `/api/blast/jobs/submit`:
  - `conflicting_query_sources` (422) when accession + inline FASTA / file / blob are both supplied.
  - `ncbi_query_too_large` (422) when the resolved FASTA exceeds the 5 MiB cap; suggests `query_accession_seq_start` / `query_accession_seq_stop`.
- No IaC changes.

## Validation evidence

- `uv run pytest -q api/tests/test_ncbi_nuccore.py api/tests/test_blast_submit_accession.py` → 49 passed (pre-edit also 49, but two tests rewritten — `test_normalise_query_data_conflicts_with_accession` + `test_normalise_query_file_conflicts_with_accession`).
- `uv run pytest -q api/tests` → 1743 passed, 3 skipped, 2 failed (`test_peering_nsg.py`, untracked WIP unrelated to this change).
- `cd web && npm test -- --run` → 394 passed (53 files).
- `cd web && npm run build` → success, 9.75s. No new chunk-size warnings beyond the pre-existing ones.
- `uv run ruff check api` → clean.

## Self-review summary

- **Consumer search**: `extractCanonicalAccession`, `internalSequenceRoute`, `ncbiNuccoreUrl`, `ncbiSearchUrl` consumers checked in `BlastHitsTable.tsx`; helpers.test.ts (22 tests) still passes. `NcbiServiceUnavailable` consumers checked — only `accession_resolver.py` previously caught it; new `NcbiResponseTooLarge` is caught before the generic `NcbiServiceUnavailable` handler.
- **Backward-compat**: New `NcbiResponseTooLarge` subclasses `NcbiServiceUnavailable` so any existing `except NcbiServiceUnavailable` still catches both — only the specific resolver short-circuits earlier.
- **Wide test sweep**: backend 1743 / 1745 (2 unrelated WIP failures), frontend 394 / 394, build clean, ruff clean.
- **Diff audit**: only intended files dirty. `web/src/pages/sequence/SequenceDetail.tsx` and `web/src/pages/blastResults/analytics/helpers.ts` are new from the prior change note; the additions here are bracketed by existing exports.
- **Fixture parity**: helpers test fixtures only cover the helpers themselves; the changed `isNcbiAccessionLike` predicate is exercised through the BlastHitsTable model test (passes). No backend mocks needed updating because the new 422 codes use the existing `HTTPException(detail=dict)` pattern.

## Items NOT included

- The two pre-existing `test_peering_nsg.py` failures belong to a separate WIP branch (`api/tasks/azure/peering_nsg.py` is currently untracked) — out of scope for this change note.
