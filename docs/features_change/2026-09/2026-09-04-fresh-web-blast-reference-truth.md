---
title: Fresh same-snapshot Web BLAST reference truth
description: Pin authoritative core_nt release evidence, taxid-bearing XML2 references, and query-specific search spaces for exact Web BLAST parity.
tags:
  - blast
  - research
---

# Fresh same-snapshot Web BLAST reference truth

## Motivation

The previous parity fixtures predated the active `core_nt` generation. Their legacy XML1
statistics exposed filtered or 32-bit-wrapped database lengths and zero effective search spaces,
which cannot prove full snapshot identity. ORF1ab also contained a grouped hit whose first visible
alias was non-excluded while three descriptors belonged to excluded-descendant taxid `2697049`.
The active-generation canonicalizer then replaced caller search space with a fixed 64-nt
calibration value even though NCBI reports query-specific values.

## User-facing change

- Fresh F3L and ORF1ab NCBI requests, XML1, XML2, and request options are pinned. The 18S refresh
  uses the same sequential, rate-polite workflow and remains required for the live three-gene gate.
- Snapshot identity is now backed by independent Web BLAST UI and NCBI v5 metadata artifacts:
  release `2026-08-19`, 130,155,243 sequences, 998,069,435,926 letters, and 84 volumes. These match
  the deployed active generation rather than relying on wrapped XML1 `db-len`.
- XML2 grouped descriptors are parsed with bounded ZIP expansion. Every descriptor must carry a
  taxid, and a pinned NCBI Taxonomy response must cover every unique result taxid.
- ORF1ab uses `NOT txid3418604[ORGN] NOT txid32630[ORGN]`. The second term removes the synthetic
  alias that caused core_nt to retain a mixed excluded/non-excluded group. The corrected reference
  contains zero taxid `3418604` descendants across 643 taxid-bearing descriptors.
- Query-specific effective search spaces survive active-generation canonicalization. The sibling
  OpenAPI scalar wire field receives a uniform value losslessly; mixed values fail closed and must
  use query-group execution. Result Passport displays the query-specific value.
- Strict comparison enriches only zero XML1 `hsp-len`/`eff-space` values from same-RID XML2. It
  does not substitute XML2 database counts for XML1's result-specific filtered statistics.
- NCBI XML1's exact modulo-$2^{32}$ filtered database length is normalized against a local 64-bit
  value only when the independent Web UI/FTP/active-generation snapshot proof is explicitly true;
  the machine report records `db_len_representation_normalized=true`.

## API and implementation summary

- `parse_xml2_deflines()` and `verify_xml2_taxid_exclusion()` provide offline all-descriptor
  taxonomy validation without title inference or network access.
- `parse_xml2_statistics()` supplies same-RID effective-space evidence.
- `ELB_PARITY_REFERENCE_DIR` may point the existing test harness at a fresh three-gene evidence
  directory; all files are mandatory once supplied.
- `query_effective_search_spaces` is accepted on the external XML API and retained in job
  provenance. Uniform values collapse only at the sibling transport boundary.
- Fast deploys now resolve newly built image tags to immutable digests before restoring private
  ACR access. This prevents a successful build from stopping before the Container App patch when
  private-network propagation blocks the later registry data-plane lookup.

## Validation

- BLAST/OpenAPI/queue baseline: 339 passed.
- Targeted parity, taxonomy, compatibility, transport, and Result Passport suites passed.
- Full backend: 5,647 passed, 5 external-input skips; Ruff clean.
- Full frontend: 110 files / 996 tests passed; ESLint and production build passed.
- Mocked fullstack Playwright: 56 passed / 6 guarded live skips after providing the missing local
  Chromium NSS/NSPR/ALSA libraries.
- Native BLAST+ 2.17.0 full versus three-shard proof: oracle concatenation exact, tabular exact,
  XML `difference_count=0`, and statistics `full_db_exact`.

Live Azure candidate IDs, App Insights evidence, and final private-network/Stopped-state cleanup
are recorded separately after the bounded live run completes.