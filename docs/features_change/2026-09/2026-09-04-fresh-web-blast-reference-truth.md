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
- OpenAPI `4.39` preserves a validated query-specific `-searchsp` in its precise active-generation
  path instead of replacing it with the 64-nt fallback. Missing values still use the active
  fallback; malformed or duplicate values fail closed. ACR run `de7w` produced digest
  `sha256:6afca07b9132a843877f1a49b2baa36b4d7da303b325f41c4741c5c533062a7a`.
- Live F3L then exposed an immutable-path init defect: the shard script resolved
  `.../generations/<id>/shards/core_nt-metadata.json`, so all ten init Jobs exhausted retries with
  exit 75 before BLAST execution. The hardened script now derives the full DB root, container-root
  metadata path, and expected generation ID directly from the immutable shard prefix. Legacy
  `<N>shards/` layouts retain their existing path behavior. The immutable generation directory is
  itself the payload root (`core_nt` is a file basename there, not another directory).
- OpenAPI `4.41` carries that final correction; ACR run `de83` produced digest
  `sha256:01c400629c0976873026dc91aa5e7b05e5e626ffdef1d20efd1cc6a69072b3eb`.
- The first complete 10/10 F3L shard run then exposed a finalizer shell bug: `azcopy` inherited
  the oracle URL manifest as stdin, consumed its remaining lines after part 0, and failed closed
  with `expected=10 downloaded=1`. Oracle part downloads now read stdin from `/dev/null`, so the
  surrounding manifest loop processes all ten URLs.
- OpenAPI `4.42` carries the oracle-loop fix; ACR run `de86` produced digest
  `sha256:3c43d992468f6e093ecbc5fff5f93047c079e6e7afb7408f1b29a95182e0fb62`.
- F3L then completed all ten BLAST shards, but strict merge correctly rejected the existing
  active oracle because 35 tax-filter-selected grouped aliases were absent. Future oracle builds
  publish oracle-v2 rows as `shard<TAB>local_oid<TAB>accession` with
  `blastdbcmd -get_dups`, so duplicate/grouped accessions share one OID rank and shard-local OID
  resets remain distinct. OpenAPI rejects v1 oracles. The existing
  `20260829192214-0757dc7b` oracle must be rebuilt before another exact live run.
- OpenAPI `4.43` carries the oracle-v2 reader/gate; ACR run `de89` produced digest
  `sha256:e76e25509f60269116be7ad5cc99e3254c4594d952f4eed00ae0ac9bd458956c`.
- Oracle dispatch recovery now handles a worker revision replacement without waiting for the
  30-minute execution deadline. It CAS-resets the exact stale execution instance only when every
  Celery worker replied and the task is absent, the heartbeat is stale, all expected Kubernetes
  Jobs are complete, and every exact run-scoped Storage part is non-empty; any uncertain
  observation preserves the claim and fails closed.
- Partitioned OpenAPI results now expose `merged_results.out.gz` as the canonical result file.
  A poll that occurs before finalization no longer caches shard `batch_*` intermediates forever;
  discovery re-lists until the merged artifact appears and then discards shard files from the
  public manifest.
- OpenAPI `4.44` carries the canonical merged-result manifest fix; ACR run `de8e` produced digest
  `sha256:d6e21281d4bddd5969cbedc59daad48d332238bb439c8239509f0af4fcf9c9ee`.
- The first `4.44` live download probe exposed a second contract mismatch: result discovery
  accepted `merged_results.out.gz`, while the download path guard still accepted only `batch_*`
  basenames. The guard now admits the exact canonical merged basename while preserving its
  traversal, query-string, extension, and arbitrary-file rejection checks.
- OpenAPI `4.45` carries the download-guard fix; ACR run `de8f` succeeded with digest
  `sha256:9aafa0767fcc3372325895dfd3dc5c9416a6a61aa5257271e85793e4a9bd4e79`, and the
  registry was restored to `publicNetworkAccess=Disabled`, `defaultAction=Deny` immediately
  after the build.

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