---
title: Harden sequence-diversity merge publication
description: Resolve merge lifecycle, partial-publication, patch migration, split-report, and ACR cancellation findings through 25 iterative hardening reviews.
tags:
  - blast
  - architecture
  - security
---

# Harden sequence-diversity merge publication

## Motivation

Removing the fixed 5,000-candidate maximum widened the valid workload range.
The request contract remained finite per shard, but larger merges made cleanup,
publication, concurrency, and report aggregation failures more expensive. An
25-round critique reviewed those boundaries until no reproducible finding above
Low severity remained.

## Runtime hardening

- The merger writes output and report artifacts to same-directory temporary
  files and exposes canonical paths only after both files are complete.
- Registered SQLite stores, journals, and temporary artifacts are removed on
  ordinary exceptions and forwarded termination signals.
- A persistent `0600` advisory lock prevents concurrent finalizers from writing
  the same canonical target. Lock waits are bounded to 30 minutes, and a waiter
  reuses a fresh completed pair from the owner instead of repeating the merge.
- Lock files are intentionally not unlinked after release. Advisory ownership
  ends when the descriptor closes; keeping the inode prevents a waiter and a
  new process from locking different files for the same target.
- Gzip output retains the canonical header filename. Atomic replacement
  preserves an existing target mode or applies the process's normal creation
  mode for a new target.
- Sequence report queries gained accession and shard/accession indexes for the
  larger valid candidate range.
- The outer ElasticBLAST finalizer still validates gzip integrity and XML shape,
  uploads output and report by their explicit names, and writes the durable
  success marker last.
- A canceled GitHub image workflow can bypass a child shell's exit trap after
  temporarily opening ACR build access. The build job now has an `always()`
  restore step, while an independent workflow-run reconciler waits for active
  ACR tasks to finish and restores `Disabled / Deny / AzureServices`. An hourly
  schedule is the final fallback if a runner itself is interrupted.
- The review reproduced that stranded `Enabled / Allow` state after canceling
  the source-only build workflow. The registry was immediately restored and
  verified at `Disabled / Deny / AzureServices` before this guard was added.

## Contract and patch hardening

- Split-parent report aggregation rejects boolean values from numeric totals
  and retains each sequence child's requested/applied pool, pool-completeness,
  and detail-truncation state. Existing non-sequence child report shape remains
  unchanged.
- The ElasticBLAST config patch fails closed unless the new non-negative
  transport guard appears exactly once and every legacy `0..5000` guard is
  absent.
- The Dashboard build-context validator requires the merge lock, cleanup, and
  owner-coalescing contracts in copied sibling source.
- The immutable source pin advances to published sibling commit
  `787b1939c334d33d37cd9b7f37da470411e027e7`; source comments describe explicit
  finite per-request pools instead of the removed fixed maximum.

## Review disposition

The 25 rounds covered request/resource admission, REST and Service Bus errors,
SQLite/disk behavior, patch migration, signal handling, concurrent finalizers,
split reports, byte and permission compatibility, and a final holistic
self-critique. Findings above Low were repaired and re-reviewed. Claims that
ignored the outer finalizer's gzip validation and success marker, Python's exact
integer JSON parsing, or the Service Bus full-payload fingerprint were rejected
against the controlling code paths.

Low residual risks remain explicit:

- A forced `SIGKILL` cannot run in-process cleanup and may leave local temporary
  files until the finalizer workspace is removed.
- Very large explicit candidate pools can consume substantial BLAST compute,
  disk, and Storage by design; the API does not restore a fixed maximum.
- Conservative filesystem timestamp checks may repeat a merge after extreme
  clock skew, but never accept an older artifact pair as fresh.
- ACR cleanup depends on GitHub OIDC and ARM availability; the independent
  hourly reconciler retries a prior failed restore without interrupting active
  builds.

## Validation

- The full sibling OpenAPI suite passes with `188 passed`.
- The full Dashboard backend suite passes with `5,837 passed, 4 skipped`; the
  skipped tests require external parity evidence directories.
- The complete unfiltered Dashboard suite passes with `5,988 passed, 4 skipped`.
- Dashboard slow and subprocess coverage passes with `151 passed`; the focused
  ACR cancellation and restoration suite contributes `19 passed`.
- Failure injection covers missing shards, report publication failure,
  termination cleanup, bounded lock timeout, duplicate-owner coalescing, and
  mixed legacy/current config guards.
- Artifact checks cover canonical gzip header naming, target mode preservation,
  all 6,000 requested groups under a 96 MiB address-space limit, and report-only
  detail truncation at 5,000.
- Ruff, the production mypy debt ratchet, the 242-operation OpenAPI contract,
  generated TypeScript drift, docs frontmatter, and strict MkDocs checks pass.
- Hardening rounds 17, 18, 22, 24, and 25 found no reproducible Medium, High, or
  Critical issue after their preceding repairs; only the documented Low
  residual risks remain.
- Host-mode API smoke passes all `27/27` checks after detached service readiness.
- No Azure image or runtime deployment is part of this source hardening.