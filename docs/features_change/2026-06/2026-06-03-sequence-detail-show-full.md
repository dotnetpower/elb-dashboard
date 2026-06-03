---
title: Sequence Detail shows full records (no display truncation)
description: Remove field/count display truncation on the Sequence Detail page so GenBank fields, features, references and FASTA render in full, while keeping the hard fetch byte caps as the safety boundary.
tags:
  - user-guide
  - blast
---

# Sequence Detail — show full, no truncation

## Motivation

The Sequence Detail page previously clipped already-fetched NCBI data for
display: long GenBank fields ended in `…` with a "view full record on NCBI"
note, only the first 200 features and first 24 references were listed, and the
FASTA preview was cut at 8 KB. Researchers had to leave the dashboard to read
the complete record. The request was to render the **full** content in-page.

## User-facing change

On `/sequence/:accession`:

- **FASTA** renders in full (the 8 KB `…(truncated, N bytes hidden)` preview cap
  is removed).
- **Features** table lists every feature (the `first 200 of N` slice and its
  "Open in NCBI to see the rest" note are removed).
- **References** lists every reference (the 24-item slice is removed).
- **GenBank fields** (`definition`, `comment`, `taxonomy_lineage`) and feature
  **qualifier values** (e.g. long `translation`) render in full; the
  "truncated — view full on NCBI" affordances no longer trigger.

What did **not** change: the backend still enforces the hard fetch byte caps
(`MAX_FASTA_BYTES` 5 MiB, `MAX_GENBANK_BYTES` 2 MiB, `MAX_SUMMARY_BYTES`) and
`MAX_FEATURES_PER_RECORD` (2000) as system-boundary safety controls against
oversized / abusive records. Removing those would be a security/stability
regression, not a display change.

## API / contract diff summary

Backend `api/services/ncbi/nuccore.py`:

- `_truncate(...)` no longer clips by length — it only normalises whitespace and
  returns the full value (the `limit` argument is retained for call-site
  compatibility but is a no-op).
- `_truncate_flagged(...)` always returns `(value, False)`. Consequently the
  response `truncated_fields` list is always `[]` and each qualifier's
  `truncated` flag is always `False`.
- Removed the per-record soft count caps that hid fetched data: qualifiers
  (was 32), references (was 24), authors (was 20), and feature intervals
  (was `[:64]`). The `MAX_FEATURES_PER_RECORD` cap stays.

The response shape is **unchanged and backward compatible**: `truncated_fields`
and qualifier `truncated` remain in the payload (now always empty / `False`), so
the frontend's contract-driven truncation affordances stay wired but never fire.
`web/src/api/ncbi.ts` keeps `truncated_fields?` optional.

Frontend `web/src/pages/sequence/SequenceDetail.tsx`:

- Removed `SEQUENCE_PREVIEW_BYTES` and the FASTA preview clip (`previewFasta`
  now returns the full FASTA).
- `genbank.features.map(...)` (was `.slice(0, 200)`) and removed the
  "Showing first 200 of N features" note.
- `genbank.references.map(...)` (was `.slice(0, 24)`).

## Validation evidence

- `uv run pytest -q api/tests/test_ncbi_nuccore.py` → 54 passed. The former
  `test_fetch_nuccore_genbank_flags_truncated_fields` was rewritten as
  `test_fetch_nuccore_genbank_returns_full_untruncated_fields`, asserting the
  5000-char comment and 600-char translation qualifier come back in full with
  `truncated_fields == []` and `truncated is False`.
- `uv run pytest -q api/tests` → 2514 passed, 3 skipped (full backend sweep,
  no other consumer of the contract broke).
- `cd web && npm run build` → built clean under TS strict.
- `uv run ruff check api/services/ncbi/nuccore.py` → clean.

## Follow-up (not in this change)

Embedding the NCBI Sequence Viewer (SViewer) in-page via NCBI's official
CORS-enabled JS widget API is feasible but carries a CSP / supply-chain and
privacy trade-off (the browser would talk to NCBI directly). It is tracked
separately rather than bundled here; the page keeps the "Open in new tab"
deep-link.
