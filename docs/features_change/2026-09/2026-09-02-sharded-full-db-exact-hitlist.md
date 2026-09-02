---
title: Exact full-DB hitlist selection for sharded BLAST
description: Reproduce native BLAST full-database subject membership and order across contiguous shards with raw-score, e-value epsilon, and database-OID comparison.
tags:
  - blast
  - architecture
---

# Exact full-DB hitlist selection for sharded BLAST

## Motivation

Proportional near-miss preservation improved visibility but could not prove
full-database equality. Investigation of the official
[NCBI C++ Toolkit BLAST core](https://github.com/ncbi/ncbi-cxx-toolkit-public/blob/main/src/algo/blast/core/blast_hits.c)
identified the actual bounded hitlist contract:

1. E-values below `1e-180` compare equal.
2. Higher raw alignment score wins next.
3. Equal e-value and raw score are ordered by descending full-database subject
   OID; when a bounded hitlist is full, a newer equal subject replaces an older
   equal subject.

The previous merge sorted by displayed bit score and shard concatenation order.
That cannot reproduce native tied-hit membership even when every required
candidate exists in the shard outputs.

## User-facing change

- **Full-DB-exact shard** replaces the earlier Web-equivalent label for precise
  sharding. Exactness means the same database snapshot and BLAST options produce
  the same subject membership, order, and HSP values as native full-DB BLAST.
- Precise submits require a ready DB-order oracle for the active database
  generation. Missing, stale, incomplete, or partially downloaded oracle data
  fails closed before BLAST submission or final merge.
- Tabular precise runs automatically include raw `score`; the merge keeps every
  HSP row for each selected subject.
- Result passports and tied-cutoff notices report full-DB exact selection. They
  no longer claim unconditional NCBI equivalence: NCBI parity additionally
  requires the same NCBI database snapshot and options.
- Approximate sharding retains the proportional near-miss policy and is clearly
  identified as heuristic rather than exact.

## API / runtime diff summary

- `terminal/merge-sharded-results.sh` distinguishes query membership oracles
  from DB-order oracles. DB-order mode uses BLAST's e-value epsilon, raw score,
  and reverse OID comparator, disables diversity reservation, and rejects any
  candidate missing from the oracle.
- XML uses `Hsp_score`; tabular precise output adds the `score` field. Existing
  tabular HSP rows and legacy report counters remain backward compatible.
- `merge-report.json` records `selection_equivalence=full_db_hitlist_exact`,
  `ranking_basis=blast_evalue_raw_score_db_oid_desc`, and oracle source.
- Dashboard precise submit normalization server-enables the DB-order oracle and
  fails before CLI execution if no same-generation oracle is ready.
- The OpenAPI build overlay validates the current oracle document, source
  version, expected shard set, and every non-empty part before privately writing
  the job's oracle manifest. No SAS URL is generated or returned to a browser.
- External API and Service Bus `core_nt` sharding profiles now normalize to the
  same precise contract as dashboard submissions.
- Multi-query tabular shard submits still require a query identity field.
- No IaC resource or dependency change is required. Runtime images must be
  rebuilt through the normal maintainer deployment path before the behavior is
  live.

## Validation evidence

- `scripts/dev/verify-local-blast-exact-sharding.sh` - passed with
  `oracle_concat_exact=true`, `tabular_exact=true`, `xml_exact=true`,
  `xml_difference_count=0`, and `xml_statistics=full_db_exact`.
- Synthetic comparator regressions cover reverse OID ties, the `1e-180` e-value
  equivalence boundary, raw-score precedence, strict query-oracle precedence,
  unmapped-candidate failure, XML, and tabular output.
- Real BLAST+ 2.17.0 proof against one 30-subject full DB and three contiguous
  10-subject shards:
  - XML: `20/20` subject order and primary e-value/raw-score/bit-score tuples
    matched exactly.
  - Tabular `6 std score`: all `20/20` data rows matched exactly.
  - Both merge reports recorded `full_db_hitlist_exact` and
    `blast_evalue_raw_score_db_oid_desc`.
- A separate real BLAST+ experiment confirmed that when 6,000 perfect subjects
  exceed `max_target_seqs=5000`, native full-DB BLAST returns 5,000 perfect
  subjects and no lower-scoring subject. This disproved proportional reservation
  as an exact policy and motivated restricting it to approximate mode.
- A real multi-HSP tabular proof retained 10 selected subjects and all 20 HSP
  rows with exact full-vs-sharded row equality.
- Fresh sibling OpenAPI context patching compiled successfully and remained
  byte-identical after a second patch application.
- Backend: `5,673 passed, 4 skipped` with slow/subprocess tests included. The
  four skips require external parity fixtures or a local sibling source clone.
- OpenAPI overlay and patcher focus: `34 passed`; fresh upstream patching also
  passed Python compile and second-application byte identity.
- Frontend: `110` files and `994` tests passed, followed by zero-warning ESLint
  and a successful production build.
- Browser mock validation showed **Full-DB exact**, the native tied-cutoff
  explanation, Methods text, and result rows at both 1440 px and 390 px widths;
  neither viewport had horizontal page overflow.
- Local API smoke: `27/27 passed`.
- Ruff lint, Python compile, shell syntax, documentation frontmatter, and
  MkDocs strict build all passed.
