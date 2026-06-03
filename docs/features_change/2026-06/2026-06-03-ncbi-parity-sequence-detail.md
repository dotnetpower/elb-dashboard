---
title: NCBI nuccore parity for the Sequence Detail page
date: 2026-06-03
tags:
  - frontend
  - ncbi
  - sequence-detail
---

# NCBI nuccore parity for the Sequence Detail page

## Motivation

A side-by-side review of the public NCBI nuccore record (e.g. `OZ254605.1`,
`NM_000546.6`) against the dashboard's Sequence Detail page surfaced 20+ fields
that NCBI shows but the dashboard either dropped after parsing or never parsed
at all. This change closes those gaps so a researcher reading the dashboard
record sees the same provenance, references, and feature qualifiers as the NCBI
page, without leaving the control plane.

## User-facing change

Sequence Detail (`/sequence/:accession`) now surfaces:

- **Molecule metadata cells** — Strandedness, Biomol, Completeness, Division,
  Source DB, and Created date are added to the header `<dl>` (previously only
  Organism / Taxid / Length / Molecule / Topology / Updated).
- **GenBank flat-file block** — the header `pre` now also renders secondary
  accessions on the `ACCESSION` line, a `GI:` `VERSION` line, and a wrapped
  `COMMENT` block when present.
- **Comment card** — a dedicated card renders the record `COMMENT` (e.g. RefSeq
  curation notes), which was parsed but previously invisible.
- **Enriched References** — each reference now shows its ordinal
  (`REFERENCE N`), consortium author, remark, and a DOI link alongside the
  existing title / authors / journal / PubMed link.
- **Expandable feature qualifiers** — every feature row can be expanded to list
  all qualifiers (`mol_type`, `isolate`, `geo_loc_name`, `db_xref`,
  `translation`, …), not just gene/product/note. `db_xref` values link to NCBI
  Taxonomy (`taxon:N`) and Gene (`GeneID:N`).
- **Copy buttons** — copy-to-clipboard controls for the accession (header) and
  the full FASTA (sequence preview card).

## API / IaC diff summary

- `api/services/ncbi/nuccore.py`: the `/ncbi/nuccore/{accession}/genbank`
  record dict gains `primary_accession`, `gi`, `other_seqids`,
  `secondary_accessions`. `_parse_references` now also returns `reference`,
  `consortium`, `doi`, and `remark` per reference. New helpers
  `_parse_other_seqids`, `_parse_secondary_accessions`, `_parse_reference_doi`.
  The route returns the dict directly (no Pydantic model), so no route/model
  change was needed.
- `web/src/api/ncbi.ts`: `NuccoreReference` gains `reference` / `consortium` /
  `doi` / `remark`; `NuccoreGenBank` gains `primary_accession` / `gi` /
  `other_seqids` / `secondary_accessions`. All new fields are nullable / array
  defaults, so existing consumers stay compatible.
- `web/src/pages/sequence/SequenceDetail.tsx`: new `CopyButton`, `FeatureRow`,
  and `FragmentQualifier` components; `dbXrefUrl` / DOI / Taxonomy / Gene link
  helpers; molecule meta cells; Comment card; enriched References; expandable
  features.
- No backend route, no Bicep, no IaC change.

## Validation evidence

- `uv run pytest -q api/tests/test_ncbi_nuccore.py` → 52 passed (fixture
  extended with other-seqids, secondary-accessions, primary-accession, and the
  reference ordinal / consortium / remark / DOI fields; new assertions added).
- `uv run pytest -q api/tests` → 2485 passed, 3 skipped. The single
  `test_terminal_exec.py::test_run_truncates_stdout_above_cap` failure under
  the full parallel run is a pre-existing flaky (timing/concurrency) test
  unrelated to this change — it passes in isolation.
- `uv run ruff check api` → all checks passed.
- `cd web && npm run build` → built successfully (`SequenceDetail` chunk
  type-checks clean).
