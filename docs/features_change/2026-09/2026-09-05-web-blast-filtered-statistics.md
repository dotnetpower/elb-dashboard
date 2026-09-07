---
title: Web BLAST taxonomy-filtered statistics parity
description: Reproduce NCBI Web BLAST taxonomy-filtered statistics and investigate exact candidate selection.
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

Validated Web BLAST statistical contexts can select the prepared one-shard `core_nt` layout for a
disk-backed candidate-selection probe. Precise requests without a Web statistical context keep the
existing parallel shard count. Runtime provenance claims exact hitlist selection only after the
execution records the monolithic topology; a planned or ordinary sharded run is not labelled exact.

## API and implementation summary

- The external submit boundary validates one query, precise `core_nt`, positive filtered counts,
  active-generation upper bounds, length adjustment, reported/scoring formulas, and a result DB
  length equal to either the filtered or active database length.
- The patched OpenAPI runtime independently repeats those checks, replaces stale `-dbsize` and
  `-searchsp` values, and writes a bounded private `web-blast-statistics.json` manifest before
  dispatch. Manifest creation is immutable: a byte-identical idempotent replay is accepted and a
  changed replay fails closed.
- BLAST+ receives `-dbsize <filtered_letters>` and `-searchsp <scoring_search_space>` before the
  search executes. HSP scores and E-values are never rewritten by the merger.
- A validated Web statistical context forces `db-partitions=1` and the immutable prepared
  `1shards/core_nt_shard_00` layout behind an explicit disk-backed mode. The database cache is
  isolated from the ordinary ten-shard cache, vmtouch is disabled, and the search receives bounded
  E16 memory resources. BLAST's `max_target_seqs` value affects preliminary candidate retention, so
  independently applying the same limit to ten shards cannot be repaired after the searches finish.
- Before dispatch, OpenAPI now cross-checks the one-shard manifest and shard NAL against the active
  generation's canonical NAL. It requires identical contiguous volume declarations, verifies the
  manifest-plus-NAL layout digest and required-byte bound, and records those values as immutable
  runtime evidence.
- The finalizer downloads the private manifest, verifies it against the active shard totals,
  runtime options, query identity, and DB-order oracle, then reconstructs only the canonical
  query-level statistics block required for a partitioned result.
- OpenAPI job payloads retain the exact oracle, validated statistical context, candidate-selection
  topology, memory mode, active-generation layout digest, taxonomy filter, candidate budget, and
  scoring profile. The dashboard projects exact provenance only after successful completion and
  only when all evidence identifies the same immutable database generation. Statistics or a planned
  one-partition topology alone never imply exact hitlist membership.
- Existing Table rows receive those two immutable runtime-evidence fields through an additive,
  idempotent payload backfill. The sync preserves unrelated payload data and refuses to overwrite
  a conflicting stored value, so a row first discovered before finalization can still converge
  after the OpenAPI job becomes terminal.

## Validation

- Native [BLAST+ 2.17.0](https://blast.ncbi.nlm.nih.gov/doc/blast-help/downloadblastdata.html)
  option matrix confirmed `-searchsp` controls HSP E-values while `-dbsize` controls the full-run
  length adjustment.
- Re-running 20 representative F3L HSP subject sequences through BLAST+ 2.17.0 with the validated
  filtered `-dbsize` and scoring `-searchsp` reproduced all 20 NCBI XML e-values exactly.
- The first 18S ten-shard run matched all query-level statistics but differed by 67 reference-only
  and 67 candidate-only accessions. The candidate-only set included high-ranking records, ruling
  out a final 500-hit tie or merge-order issue.
- NCBI 1000-hit control RID `9R6S3ZDV014` reproduced 58 of those 67 candidate-only records, including
  `EU400386` at rank 13 and `M61723` at rank 17. This demonstrates that the requested hitlist size
  changes BLAST's preliminary candidate set before traceback.
- A production-compatible unmasked MegaBLAST index built for the volume containing `M61723`
  preserved all 500 subjects and all four `M61723` HSPs. Indexed and unindexed outputs differed in
  one lower-ranked gap placement only, falsifying index use alone as the membership root cause.
- The active-generation one-shard layout contains all 84 volumes (`core_nt.00` through
  `core_nt.83`) and declares `295,616,972,515` required bytes, allowing a bounded monolithic
  candidate search on the existing 497 GB node disk.
- Earlier one-shard 18S probes used the legacy `/blast-db/1shards` layout, whose manifest stopped at
  `core_nt.79`. Those 80-volume runs, including thread, taxonomy-filter, 500-hit, and 1000-hit
  variants, are invalid acceptance evidence and their mismatch counts must not guide the final
  verdict. Only a rerun against the complete immutable 84-volume active-generation layout is valid.
- The complete-layout rerun validated `core_nt.00` through `core_nt.83`, 130,155,243 sequences,
  998,069,435,926 bases, and the active layout digest before search. Its native 500-hit result still
  differed from Web BLAST by 91 accessions in each direction, so database completeness alone is not
  sufficient.
- A native 1000-hit run contained every Web BLAST top-500 accession within its first 636 hits. The
  official `blast_formatter` 500-hit view matched native ranks 1-500 and differed from Web BLAST by
  66 accessions in each direction. A 550-candidate control, corresponding to BLAST's documented
  non-CBS internal-retention lower bound for a requested 500 hits, differed by 81 in each direction.
  Candidate over-retention and formatter separation therefore do not reproduce the Web result by
  themselves.
- All 66 native-only top-500 accessions are members of the current NCBI Nuccore
  `NOT txid5833[ORGN]` population, and neither their primary taxids nor duplicate DB deflines carry
  taxid 5833. This independently rejects a taxonomy-filter leakage explanation.
- A production-style MegaBLAST index built for one representative DB volume measured 13.50 GB for
  3.36 GB of source data. The distributed follow-up decoded all 671 contiguous index ranges for the
  active generation, merged 369,050 native preliminary HSP lists, and performed one full-database
  traceback. The result still had 67 reference-only and 67 candidate-only subjects. A separate
  84-physical-volume merge produced the same 67/67 membership drift.
- ORF1ab was intentionally not run after the complete-layout 18S comparison failed. The strict
  three-gene gate cannot pass while one required candidate is non-exact.
- The private statistics manifest uses create-only upload semantics. A byte-identical idempotent
  replay succeeds after an equality read; a changed replay fails before BLAST dispatch.
- Full backend suite, including slow/subprocess coverage: 5,791 passed, with five expected
  external-input skips.
- Full frontend suite: 110 files / 997 tests passed; ESLint and production build passed.
- Fresh three-gene reference suite: 71 passed, with four expected live-input skips.
- Ruff, docs frontmatter, and MkDocs strict build passed.
- A pristine sibling OpenAPI context was patched twice byte-identically, compiled, and contained
  exactly one active-layout validation call plus one runtime-profile evidence call.

## Exact-equivalence boundary

The cost-approved 2026-09-06 investigation did not produce a valid three-gene candidate set. F3L
remained strict exact at 358/358 subjects, but 18S remained non-exact after complete same-generation
monolithic, indexed, physical-volume, and index-range executions. Both independent Web 500 results
were equivalent to each other, while Web 500 was not the prefix of an independent Web 1000 result:
58 subjects changed, every Web-500 subject remained in Web-1000 down to rank 628, and shared-subject
relative order was preserved. This establishes a deterministic, request-hitlist-size-dependent
candidate-admission stage rather than missing sequence data or final formatting drift.

The public [Blast4 schema](https://github.com/ncbi/ncbi-cxx-toolkit-public/blob/main/src/objects/blast/blast.asn)
exposes request parameters, completed parameter defaults, request metadata, and results, but no
worker partition, preliminary admission, or cross-worker merge plan. A live BLAST+ 2.17.0
`CBlast4Client::AskGet_parameters()` probe on 2026-09-07 returned 37 descriptors (36 unique names).
Only the already-public `HitlistSize`, `FirstDbSeq`, `FinalDbSeq`, and `HspRangeMax` fields concern
candidate scope; no admission, preliminary-budget, partition, shard, worker, merge, buffer, thread,
or index control was exposed. `AskFinish_params()` for `blastn`/`megablast` returned only the default
`EvalueThreshold=10` and no hidden execution parameter. The combined response SHA-256 is
`69b26c76b329a127df723394e6d722c1ffb28b2a6bea06903ffe758950af9d46`.

The public toolkit itself points to omitted implementation paths:
[`generate_all_objects.sh`](https://github.com/ncbi/ncbi-cxx-toolkit-public/blob/main/scripts/common/impl/generate_all_objects.sh)
references `src/internal/blast/DistribDbSupport` and `src/internal/blast/SplitDB`, and the public
[`deploy.sh`](https://github.com/ncbi/ncbi-cxx-toolkit-public/blob/main/src/algo/blast/proteinkmer/demo/deploy.sh)
links Blast4 deployment procedures on an NCBI intranet URL. The public client documentation says
remote searches are spread across machines by SplitD, but no SplitD candidate-admission or merge
implementation is present in the public repositories.

No independently specified NCBI Blast4 admission contract was therefore secured. Observed budgets,
quotas, or reference-selected rules remain inadmissible implementation inputs; HSPs and XML remain
unmodified; and `candidate_engine_verified` must remain fail-closed. A new three-gene run is blocked
until NCBI supplies the SplitD admission/merge contract or an independently specified equivalent
implementation becomes available.
