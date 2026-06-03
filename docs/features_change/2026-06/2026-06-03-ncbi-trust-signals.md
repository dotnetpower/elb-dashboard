---
title: Trust and verification signals on the Sequence Detail page
date: 2026-06-03
tags:
  - frontend
  - ncbi
  - sequence-detail
---

# Trust and verification signals on the Sequence Detail page

## Motivation

The NCBI parity work made the Sequence Detail page a good *browse* surface, but a
molecular-diagnostics researcher still had to pivot back to NCBI to *verify* three
things the page could not express:

1. **Version currency** — is this accession the current `.version`, or has it been
   replaced/suppressed/withdrawn?
2. **Truncation completeness** — long fields (`translation`, `product`, `comment`,
   `definition`, taxonomy lineage) are clipped server-side, but the page gave no
   signal that the visible value was partial.
3. **Related-link precision** — symbol/organism deep links looked as authoritative
   as stable-id links, so a researcher could not tell an exact match from a text
   search that may return several records.

This change turns the page from browse-only into a verification surface without
adding any new `/api` call (the related links remain external NCBI URLs).

## User-facing change

- **Record-status trust badges** in the header:
  - `replaced` / suppressed / withdrawn / dead / unverified records show a warning
    pill; a replaced record links straight to the superseding accession when NCBI
    reports `replacedby`.
  - A `live` record shows a calm "Live record" pill.
  - A **MANE Select** keyword (parsed from the GenBank `KEYWORDS` block) shows a
    trust pill so the canonical transcript is visible at a glance.
- **Truncation "view full on NCBI" markers** wherever the backend clipped a value:
  per-feature qualifiers (`translation`/`product`), the Comment card, and the
  GenBank record card (definition / taxonomy lineage). Each marker is an external
  link to the full NCBI record.
- **Related-resource confidence tags** — links built from a stable id (GeneID,
  taxid) are treated as `exact`; links built from a gene symbol or organism string
  are tagged `search` with a tooltip noting the query may return several records.

## API / IaC diff summary

Additive only; the nuccore route returns the service dict directly, so no route or
Pydantic model changed.

- `api/services/ncbi/nuccore.py`
  - esummary parse now emits `status` and `replaced_by`.
  - New `_truncate_flagged(...) -> (clipped, truncated)` helper.
  - GenBank parse now emits a `truncated_fields: list[str]` record-level field and a
    per-qualifier `truncated: bool`.
- `web/src/api/ncbi.ts` — `NuccoreSummary.status`, `NuccoreSummary.replaced_by`,
  optional `NuccoreQualifier.truncated`, optional `NuccoreGenBank.truncated_fields`.
- `web/src/pages/sequence/SequenceDetail.tsx` — trust badges, truncation markers,
  related-link confidence tags.

No IaC change.

## Validation evidence

- `uv run pytest -q api/tests/test_ncbi_nuccore.py` → 54 passed (adds
  `test_fetch_nuccore_genbank_flags_truncated_fields` and
  `test_fetch_nuccore_summary_flags_replaced_record`, plus new asserts on the
  untruncated fixture).
- `cd web && npm run build` → clean type-check + build.
- `uv run ruff check api/services/ncbi/nuccore.py api/tests/test_ncbi_nuccore.py`
  → all checks passed.
