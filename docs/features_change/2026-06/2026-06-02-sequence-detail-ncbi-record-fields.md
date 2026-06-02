---
title: Sequence Detail page surfaces NCBI-record provenance fields
description: >-
  The Sequence Detail page now renders sample/source provenance, DBLINK
  (BioProject/BioSample), full taxonomy lineage, and references so a BLAST
  hit accession reads like its NCBI Nucleotide record page.
tags:
  - user-guide
  - blast
---

# Sequence Detail page surfaces NCBI-record provenance fields

## Motivation

A researcher inspecting a BLAST hit (e.g. `OZ254605.1`) wanted the
dashboard's per-accession view to read like the public NCBI Nucleotide
record page — i.e. show the sample provenance (isolate, geographic
location, collection date, molecule type), the DBLINK cross-references
(BioProject / BioSample), the taxonomy lineage, and the publication
references. The BLAST results table already deep-links each accession to
`/sequence/{accession}` (`SequenceDetail`), but that page only rendered a
small header (organism / length / molecule / topology / updated) plus a
features table — and the features table's *Gene / Product* column was
silently broken.

## User-facing change

On the Sequence Detail page (`/sequence/{accession}`):

- **New "Sample & source" card** — pulls the `source` feature qualifiers
  NCBI shows: `mol_type`, `isolate`, `strain`, `host`, geo location
  (`geo_loc_name`, falling back to the legacy `country` qualifier),
  `collection_date`, `collected_by`, `isolation_source`. Only the
  qualifiers actually present are rendered.
- **New DBLINK row** — BioProject / BioSample (and any other
  `GBSeq_xrefs` entries), with outbound links to the NCBI BioProject /
  BioSample pages.
- **New "Taxonomy" card** — the full lineage from `GBSeq_taxonomy`,
  rendered as a breadcrumb (`Eukaryota › Metazoa › … › Homo`).
- **New "References" card** — publication title, authors, journal, and a
  link to PubMed when a PMID is present.
- **Bug fix** — the features table *Gene / Product* column now resolves
  correctly. The frontend type declared `qualifiers` as a string map, but
  the backend returns an ordered `[{name, value}]` list (to preserve
  duplicate `db_xref` entries), so `feature.qualifiers.gene` was always
  `undefined` and every row showed `—`.

No new page, route, or navigation entry. The data is fetched on demand via
the existing `GET /api/ncbi/nuccore/{accession}/genbank` endpoint when a
hit accession is opened, so the NCBI 3 req/s budget is unchanged (one
GenBank fetch per opened accession, 24h cached).

## API / IaC diff summary

- **Backend** [api/services/ncbi/nuccore.py](../../../api/services/ncbi/nuccore.py):
  added `_parse_xrefs()` and an `xrefs: list[{dbname, id}]` field to the
  parsed GenBank payload (parsed from `GBSeq_xrefs`, i.e. the record-level
  DBLINK block). Additive — no existing field changed.
- **Frontend types** [web/src/api/ncbi.ts](../../../web/src/api/ncbi.ts):
  corrected `NuccoreFeature.qualifiers` to `NuccoreQualifier[]`,
  `NuccoreGenBank.taxonomy_lineage` to `string` (it is a semicolon-joined
  string, not an array), `NuccoreReference` to the real backend shape
  (`{title, journal, authors: string[], pubmed}`), and added
  `NuccoreGenBankXref` + `NuccoreGenBank.xrefs`.
- **Frontend** [web/src/pages/sequence/SequenceDetail.tsx](../../../web/src/pages/sequence/SequenceDetail.tsx):
  qualifier-lookup helper, source/DBLINK/taxonomy/references cards, and
  the Gene/Product column fix.
- **No** IaC, Bicep, or Container App changes.

## Validation evidence

- `uv run pytest -q api/tests/test_ncbi_nuccore.py` → 52 passed (includes
  the new `xrefs` assertion in `test_fetch_nuccore_genbank_parses_record`).
- `uv run ruff check api/services/ncbi/nuccore.py api/tests/test_ncbi_nuccore.py`
  → all checks passed.
- `cd web && npm run build` → type-check + production build succeeded
  (`SequenceDetail-*.js` emitted, no TS errors).
