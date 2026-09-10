---
title: Harden sequence-diversity merge publication
description: Resolve merge lifecycle, disk admission, patch migration, split-report, and ACR cancellation findings through 31 iterative hardening reviews.
tags:
  - blast
  - architecture
  - security
---

# Harden sequence-diversity merge publication

## Motivation

Removing the fixed 5,000-candidate maximum widened the valid workload range.
The request contract remained finite per shard, but larger merges made cleanup,
publication, concurrency, and report aggregation failures more expensive. A
31-round critique reviewed those boundaries until no reproducible finding above
Low severity remained.

## Runtime hardening

- The merger writes output and report artifacts to same-directory temporary
  files and exposes canonical paths only after both files are complete.
- Registered [SQLite](https://www.sqlite.org/docs.html) stores, journals, and
  temporary artifacts are removed on ordinary exceptions and forwarded
  termination signals. A later same-target run also removes artifacts left by
  an uncatchable `SIGKILL` while preserving unrelated and similarly prefixed
  merge targets.
- A persistent `0600` advisory lock prevents concurrent finalizers from writing
  the same canonical target. Lock waits are bounded to 30 minutes, and a waiter
  reuses a fresh completed pair from the owner instead of repeating the merge.
- Lock files are intentionally not unlinked after release. Advisory ownership
  ends when the descriptor closes; keeping the inode prevents a waiter and a
  new process from locking different files for the same target.
- Owner completion uses a UUID generation record plus exact output/report sizes
  and totals instead of filesystem timestamps, so clock skew cannot accept or
  reject a canonical pair incorrectly.
- Gzip output retains the canonical header filename. Atomic replacement
  preserves an existing target mode or applies the process's normal creation
  mode for a new target.
- Sequence report queries gained accession and shard/accession indexes for the
  larger valid candidate range.
- Merge admission measures free disk, reserves 64–512 MiB for process health,
  estimates the SQLite working set from the input size, and applies a SQLite
  page ceiling. Reports expose bounded disk-pressure and per-shard saturation
  evidence; saturation details stop at 100 while the exact saturated-shard
  count remains available.
- The outer ElasticBLAST finalizer still validates gzip integrity and XML shape,
  uploads output and report by their explicit names, and writes the durable
  success marker last.
- A canceled [GitHub Actions](https://docs.github.com/en/actions) image workflow
  can bypass a child shell's exit trap after temporarily opening
  [Azure Container Registry](https://learn.microsoft.com/azure/container-registry/)
  build access. The build job now has an `always()` restore step, while an
  independent workflow-run reconciler waits for active ACR tasks to finish and
  restores `Disabled / Deny / AzureServices`. The fallback now runs every 15
  minutes, requires 180 seconds of continuous idle state, and rechecks the
  opened policy after propagation settling. Its concurrency group remains
  separate from Build Images so GitHub cannot discard an older pending build.
- The review reproduced that stranded `Enabled / Allow` state after canceling
  the source-only build workflow. The registry was immediately restored and
  verified at `Disabled / Deny / AzureServices` before this guard was added.

## Contract and patch hardening

- Split-parent report aggregation rejects boolean values from numeric totals
  and retains each sequence child's requested/applied pool, pool-completeness,
  disk-pressure metrics, bounded saturation details, and detail-truncation
  state. Integer-valued JSON numbers are normalized to integers in both totals
  and child summaries; sequence boolean fields reject non-boolean encodings.
  Existing non-sequence child report shape remains unchanged.
- Service Bus durable job snapshots retain the selection policy and candidate
  pool. The Playground exposes the sequence-diversity preset, permits explicit
  pools above 5,000, omits a blank pool so the server applies
  `max(2,000, max_target_seqs)`, and blocks a pool below `max_target_seqs`.
- The ElasticBLAST config patch fails closed unless the new non-negative
  transport guard appears exactly once and every legacy `0..5000` guard is
  absent.
- The Dashboard build-context validator requires the merge lock, cleanup, and
  owner-coalescing contracts plus the bounded saturation and disk-admission
  markers in copied sibling source.
- The immutable source pin advances to published sibling commit
  `6132ccba35714c77ee724642697674ec3cf975e3`; source comments describe explicit
  finite per-request pools instead of the removed fixed maximum.

## Review disposition

The 31 rounds covered request/resource admission, REST and Service Bus errors,
SQLite/disk behavior, patch migration, signal handling, concurrent finalizers,
split reports, byte and permission compatibility, ACR pre-registration races,
GitHub pending-run semantics, and a final holistic self-critique. Findings above
Low were repaired and re-reviewed. Claims that
ignored the outer finalizer's gzip validation and success marker, Python's exact
integer JSON parsing, or the Service Bus full-payload fingerprint were rejected
against the controlling code paths.

Only external or intentionally requested Low residual risks remain:

- An uncatchable `SIGKILL` may leave scoped local files until the next
  same-target run or ephemeral workspace deletion; the next run removes them
  under the canonical lock.
- Very large explicit candidate pools intentionally consume more BLAST compute
  and Storage. Merge disk use is admitted and bounded without reinstating a
  fixed request maximum.
- ACR cleanup still depends on GitHub OIDC and ARM availability. The immediate
  restore plus independent 15-minute recovery path retries after either plane
  returns and never closes the registry while an active build is observable.

## Validation

- The full sibling OpenAPI suite passes with `192 passed`.
- The full Dashboard backend suite passes with `5,853 passed, 4 skipped`; the
  complete unfiltered suite passes with `6,010 passed, 4 skipped`. The skipped
  checks require external parity evidence directories.
- The affected Dashboard backend and subprocess sweep passes with `531 passed`;
  the focused ACR opening/restoration suite contributes `36 passed`.
- Frontend validation passes with `1,024` unit tests, a strict TypeScript/Vite
  production build, ESLint, and a real Chromium Playground scenario covering a
  blank pool, invalid pool, and explicit pool of 20,000.
- Failure injection covers missing shards, report publication failure,
  termination cleanup, bounded lock timeout, duplicate-owner coalescing, and
  mixed legacy/current config guards.
- Artifact checks cover canonical gzip header naming, target mode preservation,
  all 6,000 requested groups under a 96 MiB address-space limit, and report-only
  detail truncation at 5,000.
- Ruff, the production mypy debt ratchet, the 242-operation OpenAPI contract,
  generated TypeScript drift, docs frontmatter, and strict MkDocs checks pass.
- Final review rejected a false signal-forwarding finding against the existing
  PID capture/pending-signal code and repaired the reproducible producer-detail,
  ACR pre-registration, GitHub pending-run, prefix-cleanup, and child numeric
  projection findings. No reproducible Medium, High, or Critical issue remains.
- Host-mode API smoke passes all `27/27` checks after detached service readiness.
- The 53-case owner/contributor/reader/dev-bypass persona matrix passes. There
  is no persona, RBAC, route-auth, Reader allowlist, or IaC change.
- The deployed API sidecar's managed identity passed read-only Blob container,
  Table, ACR registry, and Container App probes from inside the private network.
  AKS and Key Vault probes were optional skips because those resource names are
  not configured in the current sidecar environment.
- No Azure image or runtime deployment is part of this source hardening.