---
title: NCBI fetch accepts PDB structure-chain accessions
description: The Generate-query-from-NCBI Load genes / Insert sequence flow no longer rejects PDB structure-chain accessions (e.g. 8WGZ_T) that the db=nuccore search itself returns.
tags:
  - blast
  - user-guide
---

# NCBI fetch accepts PDB structure-chain accessions

## Motivation

In the New Search **Generate query from NCBI** modal, searching `mpox` (db=nuccore)
returned PDB structure chains such as `8WGZ_T` (the 5-bp ssDNA chain T embedded in
the MPOX E5 hexamer structure). Selecting one and pressing **Load genes** /
**Insert sequence** failed with:

> accession is not a recognisable NCBI identifier

The fetch-path validator (`normalise_accession`) only accepted GenBank/RefSeq
accession shapes (`[A-Z]{1,4}_?[0-9]+...`), so a digit-led PDB ID + chain
(`8WGZ_T`) was rejected even though the same search legitimately surfaced it —
an inconsistency between what the search returns and what the fetch accepts.

## User-facing change

* `GET /api/ncbi/nuccore/{accession}` (summary / genbank / fasta) and the modal's
  Load genes / Insert sequence now accept PDB structure-chain accessions of the
  form `<4-char digit-led PDB ID>_<chain>` (e.g. `8WGZ_T`, `1ABC_AB`), in
  addition to the existing GenBank/RefSeq shapes.
* The conservative rejection of arbitrary strings is unchanged — only the PDB
  chain shape was added, still capped at 32 characters and still upper-cased.

## API/IaC diff summary

* [api/services/ncbi/nuccore.py](../../../api/services/ncbi/nuccore.py) — added
  `_PDB_ACCESSION_RE` + an `_is_recognised_accession()` predicate used by both
  the pipe-extraction loop and the final check in `normalise_accession`.
* No frontend change (the modal forwards the accession unchanged; the error
  originated in the backend validator).

## Validation evidence

* `uv run pytest -q api/tests/test_ncbi_nuccore.py` → 65 passed (new accepts:
  `8WGZ_T`, `8wgz_t`→`8WGZ_T`, ` 8WGZ_T `, `1ABC_AB`).
* `uv run ruff check api/services/ncbi/nuccore.py` → clean.
