---
title: Web BLAST taxonomy-filtered statistics parity
description: Reproduce NCBI Web BLAST taxonomy-filtered database statistics and HSP E-value scoring in precise sharded execution.
tags:
  - blast
  - research
  - contributor
---

# Web BLAST taxonomy-filtered statistics parity

## Motivation

A same-snapshot [NCBI Web BLAST](https://blast.ncbi.nlm.nih.gov/) F3L comparison matched all 358
subjects, their order, and every alignment field, but 321 nonzero HSP E-values and two result
statistics differed. The deployed candidate reported the active full database count
(`130,155,243`) and shard-derived length adjustment (`33`). NCBI reported the taxonomy-filtered
count (`130,118,804`) and full-search length adjustment (`36`).

The NCBI result page also exposed a dual-space contract that a scalar `-searchsp` alone cannot
reproduce:

- Reported effective space:
  `(query_length - L) * (filtered_letters - filtered_sequences * L)`.
- HSP scoring space:
  `(query_length - L) * filtered_letters`.

For F3L those values are `421,817,959,873,974` and `423,813,461,852,118`, respectively. The
observed NCBI/candidate E-value ratio matches the ratio of those spaces.

## User-facing change

Precise single-query `core_nt` submissions can carry a validated
`web_blast_statistical_context`. The Result Passport distinguishes the reported effective search
space from the taxonomy-filtered scoring space used for HSP E-values. Existing requests without
this context retain their prior behavior.

The fresh 18S reference is now RID `9N5JA17Y014`, with same-RID XML1/XML2 and all-defline NCBI
Taxonomy evidence. All 531 XML2 deflines carry taxids, and none is taxid `5833` or a descendant.

## API and implementation summary

- The external submit boundary validates one query, precise `core_nt`, positive filtered counts,
  active-generation upper bounds, length adjustment, reported/scoring formulas, and a result DB
  length equal to either the filtered or active database length.
- The patched OpenAPI runtime independently repeats those checks, replaces stale `-dbsize` and
  `-searchsp` values, and writes a bounded private `web-blast-statistics.json` manifest before
  dispatch. Manifest creation is immutable: a byte-identical idempotent replay is accepted and a
  changed replay fails closed.
- BLAST+ receives `-dbsize <filtered_letters>` and `-searchsp <scoring_search_space>` before each
  shard executes. HSP scores and E-values are never rewritten by the merger.
- The finalizer downloads the private manifest, verifies it against the active shard totals,
  runtime options, query identity, and DB-order oracle, then reconstructs only the canonical
  query-level statistics block required for a partitioned result.
- OpenAPI job payloads retain the exact oracle and validated statistical context. The dashboard
  projects them into provenance only when both are present, allowing UI, API, citation export,
  and Result Passport to share one evidence source without claiming exactness for incomplete
  jobs.

## Validation

- Native [BLAST+ 2.17.0](https://blast.ncbi.nlm.nih.gov/doc/blast-help/downloadblastdata.html)
  option matrix confirmed `-searchsp` controls HSP E-values while `-dbsize` controls the full-run
  length adjustment.
- Re-running 20 representative F3L HSP subject sequences through BLAST+ 2.17.0 with the validated
  filtered `-dbsize` and scoring `-searchsp` reproduced all 20 NCBI XML e-values exactly.
- The private statistics manifest uses create-only upload semantics. A byte-identical idempotent
  replay succeeds after an equality read; a changed replay fails before BLAST dispatch.
- Full backend suite, including slow/subprocess coverage: 5,791 passed, with five expected
  external-input skips.
- Full frontend suite: 110 files / 997 tests passed; ESLint and production build passed.
- Fresh three-gene reference suite: 71 passed, with four expected live-input skips.
- Ruff, docs frontmatter, and MkDocs strict build passed.
- A pristine sibling OpenAPI context was patched twice byte-identically, compiled, and passed
  shell syntax validation (`context_sha256=23f24f9c1a5bed1c5358f83b7b80c714fdd37175513cb8c9b99b43f76ff2b8ef`).

Live OpenAPI image digest, serial F3L/18S/ORF1ab candidate IDs, exact comparator reports, UI/API
and export checks, telemetry window, and final Azure cleanup state are appended after the live run.
