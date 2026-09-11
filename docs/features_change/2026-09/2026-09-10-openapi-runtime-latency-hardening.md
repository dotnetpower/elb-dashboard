---
title: Reduce OpenAPI BLAST completion latency
description: Remove per-job full database-order scans, attest reusable node-local shards, and make external job timing and completion states durable and accurate.
tags:
  - blast
  - architecture
  - ui
---

# Reduce OpenAPI BLAST completion latency

## Motivation

Repeated `core_nt_safe` requests spent only seconds in the distributed search but roughly six
minutes in result finalization. Each finalizer downloaded and scanned the complete 3.27 GiB
DB-order oracle even though the merged shard outputs contained only a bounded candidate set.
The same jobs also repeated node-local SSD validation and the dashboard folded queue wait into
its displayed duration when an in-memory timing cache was empty.

## User-facing change

- Exact DB-order finalization now prefers a job-scoped candidate oracle generated while each
  shard holds its database reader lock. Sparse local OIDs and aliases retain the same comparator
  result as the complete oracle for XML and tabular output.
- Missing, malformed, oversized, incomplete, or timed-out candidate artifacts fail safely to the
  existing generation-wide oracle. Fast-path and fallback logs include file, row, byte, and reason
  evidence.
- Reusable node-local shards carry a source/layout/manifest/file-stat attestation backed by a full
  record-stream probe. An unchanged immutable generation can skip remote metadata and record scans
  for 24 hours; expiry or any mismatch runs the original validation and repair path.
- External OpenAPI jobs remain `running / finalizing` until the current ElasticBLAST runtime
  identity's durable `SUCCESS.txt` marker exists. Parseable shard files alone no longer make a job
  appear complete.
- Queue wait, runtime, and elapsed timing are validated and persisted in
  [Azure Table Storage](https://learn.microsoft.com/azure/storage/tables/table-storage-overview),
  so a process restart does not make the UI count queue time as execution time.
- Direct sibling `/v1/jobs` submissions display as `api`; only API submissions carrying a
  dashboard correlation ID display as `api (dashboard)`.
- Artifact identity waits expire after 30 minutes and one runtime generation receives at most five
  periodic reconciliation attempts. Exhaustion is visible as a failed artifact sentinel and a new
  runtime identity can still recover.
- OpenAPI now persists a deterministic canonical ElasticBLAST runtime ID before starting submit and
  invokes the CLI through its JSON idempotency path. A pod restart resumes that runtime instead of
  creating another generation. Legacy in-flight submits recover the latest matching runtime from
  Kubernetes Job result paths; a failed Kubernetes observation never counts as proof of absence.

## Implementation summary

- `terminal/patch_elastic_blast.py` patches the [AKS](https://learn.microsoft.com/azure/aks/)
  shard runner, result exporter, and finalizer with candidate-order generation, bounded transfer,
  validation, and full-oracle fallback.
- `terminal/merge-sharded-results.sh` records whether the DB-order oracle scope was `candidate` or
  `full` in the merge report.
- `api/services/warmup/scripts.py` and the OpenAPI runtime patch share the immutable-generation
  cache-attestation contract.
- External job sync, webhook, stale-job reconciliation, and artifact reconciliation now share
  validated timing, runtime identity, and terminal-state bounds.
- The BLAST Jobs source label and tooltip use the durable submission/correlation metadata.

## Hardening review

Fourteen focused review rounds covered exact comparator equivalence, aliases and sparse OIDs,
partial uploads, aggregate bounds, subprocess deadlines, cache generation fencing, marker
publication, timing validation, terminal-state convergence, artifact retry liveness, concurrency,
security boundaries, rolling fallback, and runtime-identity binding. The fourteenth round used the
live rollout to find and fix a High-severity restart replay: four request IDs each had three distinct
runtime generations because their blocking submit threads died before returning the generated ID.
All reproducible findings above Low severity were fixed and revalidated. The remaining Low risk is
that the 24-hour shard attestation fingerprints file metadata rather than every byte;
source/layout identity, size, mtime, ctime, `blastdbcmd -info`, and the bounded full record probe
limit that exposure.

## Validation

- `50 passed`: complete sharded merge suite including XML/tabular full-versus-candidate oracle
  equivalence.
- `108 passed`: terminal runtime patcher and OpenAPI build-context patcher suites.
- `145 passed`: warmup, artifact liveness, and external webhook suites.
- `17 passed`: BLAST Jobs frontend tests.
- Hermetic backend sweep: `5,822 passed, 4 skipped`; the remaining tests in
  `test_ncbi_nuccore.py` passed `64/64` after excluding one independently reproduced pre-existing
  dev-bypass rate-bucket failure.
- Full frontend sweep: `1,024 passed`; production build and ESLint passed.
- Mypy debt ratchet: passed and reduced from 502 to 500 errors across 97 production files.
- OpenAPI contract: unchanged at 242 operations; generated TypeScript types are current.
- Documentation frontmatter and strict MkDocs build: passed.
- `npm --prefix web run build`: passed.
- `uv run ruff check api scripts/dev/patch-openapi-build-context.py terminal/patch_elastic_blast.py`:
  passed.
- Current OpenAPI build context accepted two consecutive patch applications and all generated shell
  scripts passed `bash -n`.
- Replay-safe OpenAPI build-context patcher suite: `47 passed`; Ruff passed. The patched sibling
  context was applied twice without drift and all generated Python modules passed `py_compile`.
- ACR run `de9y` built immutable `elb-openapi:4.57` successfully with digest
  `sha256:9f8fc4aa59c552cd77681df445a3736056f655d6b8be553f43aafd42a52a92fb`.
- After the build, the registry returned to `publicNetworkAccess=Disabled`,
  `defaultAction=Deny`, and `networkRuleBypassOptions=AzureServices`.
- AKS rollout generation 58 pulled the exact `4.57` digest and reached 1/1 Ready with zero restarts;
  authenticated `/v1/ready` reported 10 ready workload nodes. Startup recovery exposed the replay
  defect above, so `4.57` remains diagnostic and is not the final rollout image.

Live deployment evidence will record the candidate fast-path byte count, attested SSD reuse, and
end-to-end canary timing after the new OpenAPI image is rolled out.
