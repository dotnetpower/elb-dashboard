---
title: Separate shard candidate and result limits
description: Preserve a widened strict-oracle candidate pool without increasing the requested final BLAST result count.
tags:
  - blast
  - user-guide
  - architecture
---

# Separate shard candidate and result limits

## Motivation

Strict query-oracle [ElasticBLAST](https://blast.ncbi.nlm.nih.gov/doc/elastic-blast/)
execution can widen each shard's `max_target_seqs` to retain subjects needed by the final merge.
The submit task already preserved the caller's original limit as
`requested_max_target_seqs`, but split-child filtering and the ElasticBLAST runtime discarded that
field. The merger therefore had no independent final-result cap.

The investigation also recovered 39 completed 5,000-hit jobs from the deployed job index. Every
job retains its query batch, ten shard outputs, merged output, and merge report. The legacy reports
show no diversity reservation; several record 45,000 tied subjects outside the selected window,
which is direct replay evidence for the reported pre-fix behavior.

Replaying the retained shard outputs with the deployed proportional merger completed for all 39
jobs. Six changed result rows while remaining capped at 5,000 subjects. Four jobs replaced 13,195
subjects and restored 822 previously omitted distinct `sseq` values. The two long-row jobs that
initially OOM-killed the terminal-sidecar merger preserved the same 5,000 subjects while restoring
three additional HSP sequence values each. Across the corpus, 828 distinct sequence values were
restored with zero previously present distinct values lost.

## User-facing change

- A widened strict-oracle candidate pool no longer increases the final result limit.
- `merge-report.json` records `candidate_pool_size` separately from the final
  `max_target_seqs`.
- Tabular merging stores input-file offsets instead of complete `qseq`/`sseq` rows in memory, then
  spools rank metadata through [SQLite](https://www.sqlite.org/), and seeks only the rows belonging
  to selected subjects. Ranking and output order are unchanged.
- Full-DB order-oracle files are streamed line by line. Only ranks for accessions present in the
  shard candidate pool remain in memory; query-specific oracles retain their prior semantics.
- Requests without an internal widened pool retain their existing behavior.
- Approximate proportional reservation and precise DB-order selection remain separate contracts.
  Neither mode claims NCBI Web equality without the existing strict evidence gate.

## API and runtime summary

- The internal `requested_max_target_seqs` field survives split-child option filtering.
- Generated ElasticBLAST configuration writes `requested-max-target-seqs` separately from the
  BLAST command's widened `-max_target_seqs` value.
- The patched ElasticBLAST config model passes the optional value to the finalizer as
  `ELB_REQUESTED_MAX_TARGET_SEQS`.
- The merger rejects non-integer, non-positive, or candidate-pool-exceeding final caps.
- The shared submit contract rejects a Web BLAST statistical context combined with a query-specific
  tie-order oracle. These are distinct exactness contracts and cannot be composed safely.
- No public request field or response field was removed or renamed.

## Validation

- Final merger suite: 37 passed, including tabular/XML cap separation and both bounded-memory
  regressions.
- OpenAPI build-context and ElasticBLAST patch-chain suites: 77 passed; applying the patcher twice
  produced byte-identical output.
- The shared submit-contract regression rejects the incompatible Web-statistics/query-oracle
  combination for both dashboard and Service Bus sources.
- Real retained corpus: 39/39 jobs merged with the final implementation. Six jobs changed result
  rows and restored 828 distinct sequence values with zero prior distinct values lost. The final
  two-job replay retained 5,000 subjects per job, added three HSP sequence values per job, and
  produced summary SHA-256
  `ed58fe6b70b65f42714484c4f0ad86ccfe2d9a697562917a05512d7b04ed6116`.
- Bounded-memory probe: an approximately 79 MiB tabular input with 5,000 16-KiB sequence rows
  merged under a 96 MiB address-space limit; peak RSS was 20.6 MiB. The previous implementation
  failed the same command with `MemoryError`.
- A one-million-row DB-order oracle completed under the same 96 MiB limit while preserving reverse
  OID tie order. The deployed oracle is approximately 6.5 GiB; its live canary used approximately
  15 MiB Python RSS while the remaining cgroup usage was reclaimable file cache.
- Live [Azure Kubernetes Service (AKS)](https://learn.microsoft.com/azure/aks/what-is-aks)
  incident recovery completed in two stages. Six legacy finalizers that reached 15-40 GiB RSS were
  recovered after the SQLite conversion. A later precise-search wave exposed the separate oracle
  allocation, with nine finalizers reaching 19-41 GiB; OpenAPI was paused, active finalizers were
  suspended, and all ten jobs in that wave subsequently completed with the streaming fix.
- After recovery, workload-node memory was 10-25%, every node reported `MemoryPressure=False`, and
  no OOM event occurred after the final restart window.
- Final OpenAPI image `elb-openapi:4.50` built successfully in
  [Azure Container Registry (ACR)](https://learn.microsoft.com/azure/container-registry/container-registry-intro)
  run `de8r`, digest
  `sha256:4d837a0fab027242df118ddce776df07fa0e0657e70adfbc15dee2c537927f5e`.
  The embedded merger SHA-256 is
  `c362535f0f85b0982cba43c0d48a0e82b63fce78422fb21a816e18685e513c52`.
- OpenAPI 4.50 deployed at one ready replica with `/healthz=200`, zero container restarts, and
  byte-identical checkout, image-embedded, and live ConfigMap merger scripts.
- ACR returned to `publicNetworkAccess=Disabled` and `defaultAction=Deny` after the build.
- Full backend validation, including slow and subprocess tests: 5,838 passed and five opt-in
  external-fixture tests skipped. Ruff and ShellCheck passed.
- Frontend validation: 999 tests passed and the production TypeScript/Vite build completed.