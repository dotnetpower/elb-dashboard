---
title: Make database warmup transfer failures terminal and resumable
description: Preserve failed AKS warmup Jobs, avoid re-downloading completed shard files, and align warmup execution deadlines.
tags:
  - blast
  - operate
  - architecture
---

# Make database warmup transfer failures terminal and resumable

## Motivation

A live `core_nt` warmup created ten node-pinned Jobs on [Azure Kubernetes
Service](https://learn.microsoft.com/azure/aks/what-is-aks). Seven Jobs
completed and three reached their one-hour deadline, but the dashboard continued
to show `7/10 · Loading`. The failed Pods had already been deleted, and pod-log
enrichment replaced the authoritative Job failure count with the seven Pods it
could still observe.

The same run also exposed transfer amplification. A failed
[AzCopy](https://learn.microsoft.com/azure/storage/common/storage-use-azcopy-v10)
wildcard attempt downloaded every file in the shard again on its next attempt.
The [Azure Storage](https://learn.microsoft.com/azure/storage/common/storage-introduction)
account emitted approximately 4.34 TiB during a run whose ten node-local shard
caches totalled roughly 350–380 GiB. Pod logs reported that disk speed was the
limiting factor while ten unconstrained AzCopy processes ran concurrently.

## User-facing change

- A failed warmup Job remains `Failed` even when its failed Pod and log are no
  longer available. Missing Pods are not reclassified as active work.
- A failed Pod does not fail the database while the Kubernetes Job controller
  is using its allowed retry; only a terminal `Failed=True` Job condition does.
- Warmup retries keep completed local files and copy only files whose Storage
  source is newer, reducing repeated data transfer and recovery time.
- The production warmup task now defaults to 64 AzCopy connections per node.
  `WARMUP_AZCOPY_CONCURRENCY` remains the operator override.
- Warmup status polling outlives the Kubernetes Job deadline, and the per-task
  [Celery](https://docs.celeryq.dev/en/stable/) limits outlive that poller. A
  startup invariant rejects configurations whose task limit would outlive the
  stale-row safety thresholds.

## API and infrastructure diff summary

- No HTTP route or response field changed. Existing `Ready`, `Loading`,
  `Failed`, and `Unknown` values retain their public meaning.
- `attach_pod_progress_to_database_status()` now merges Pod phases
  monotonically into authoritative Job counts instead of replacing them.
- Job aggregation distinguishes a failed Pod counter from a terminal Job
  failure condition.
- The warmup shard copy adds `--overwrite=ifSourceNewer` to its bounded
  three-attempt retry loop.
- `warmup_database` supplies bounded AzCopy concurrency and declares task-local
  soft/hard time limits above the Job polling ceiling.
- No Bicep, role assignment, network ACL, managed identity, or Storage public
  access setting changed.

## Validation evidence

- Focused warmup, Kubernetes status, and auto-stop tests: `118 passed`.
- Full backend suite: `4807 passed, 4 skipped`.
- Regression fixture reproduces the live `7 succeeded / 3 DeadlineExceeded`
  state with only seven surviving Pods and verifies the result remains
  `Failed`, with `nodes_failed=3` and `nodes_active=0`.
- Generated-script assertion verifies the resumable overwrite policy.
- Live Blob-to-local probe in the deployed terminal sidecar copied an 88-byte
  shard manifest once, then repeated the same command with
  `--overwrite=ifSourceNewer`: the second run reported `0 Done, 1 Skipped` and
  `Total Number of Bytes Transferred: 0`. The temporary probe file was removed.
- Task contract assertion verifies `Job deadline < poll ceiling < Celery soft
  limit < Celery hard limit < stale-row thresholds`.
- Ruff lint and format checks passed on all touched Python files.
- The generated warmup script passed `bash -n` syntax validation.
- Documentation frontmatter guard and `mkdocs build --strict` passed.
