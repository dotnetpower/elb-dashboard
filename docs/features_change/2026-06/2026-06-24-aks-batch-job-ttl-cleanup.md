---
title: Auto-delete completed BLAST batch Jobs via ttlSecondsAfterFinished
description: The terminal sidecar now injects a Job-level TTL into the AKS batch-job templates so finished blastn-batch Jobs are garbage-collected instead of accumulating on the persistent cluster.
tags:
  - blast
  - terminal
---

# 2026-06-24 — Auto-delete completed BLAST batch Jobs (TTL)

## Motivation

On the persistent dashboard AKS cluster, finished `blastn-batch-*` Jobs were
never garbage-collected. A live customer cluster had accumulated **2,260**
completed batch Jobs (2-3 days of runs), plus **226** completed `elb-finalizer-*`
Jobs accumulating the same way (one per completed search). Completed Jobs hold
no node RAM/CPU, but the unbounded backlog inflates etcd / API list latency and
was previously implicated in node ephemeral-storage / DiskPressure pressure.

Upstream `dotnetpower/elastic-blast-azure` already fixed this in commit
`ba8075b1` (2026-06-19, "feat(aks): auto-delete completed BLAST jobs via
ttlSecondsAfterFinished"), which adds `ttlSecondsAfterFinished` to the
batch-job templates. The dashboard terminal sidecar pins an older elastic-blast
ref (`f4b8b734`, 2026-05-22) that predates the fix, so the TTL was never
rendered — every live Job had `ttlSecondsAfterFinished` unset.

A simple ref bump is not viable: upstream removed the `bin/` launcher in
`72a69822` (2026-06-13), *before* the TTL commit, so any ref containing the TTL
fix fails the terminal image build (no `elastic-blast` executable).

## User-facing change

- Completed BLAST batch Jobs (and their pods) are now auto-deleted **30 minutes**
  after finishing, by the Kubernetes TTL-after-finished controller. The backlog
  no longer grows without bound.
- **No change to the dashboard Blast Jobs experience.** Job listing reads the
  persisted jobstate Azure Table, job detail/execution-steps read the Table plus
  persisted artifact blobs, and results stream from Storage blobs. None of these
  depend on the live k8s Job, so deleting a finished Job does not affect listing,
  detail, logs, or result retrieval.

## API / IaC diff summary

- `terminal/patch_elastic_blast.py`: new `patch_aks_job_ttl(root)` ports the
  upstream behaviour onto the pinned ref. It injects a **literal**
  `ttlSecondsAfterFinished: <N>` at the `Job.spec` level into the three
  batch-job templates (`blast-batch-job-aks`, `blast-batch-job-local-ssd-aks`,
  `blast-batch-job-shard-ssd-aks`) **and** the finalizer template
  (`elb-finalizer-aks` — same accumulation problem, beyond upstream's scope). A
  literal value (not the upstream `${ELB_JOB_TTL_SECONDS}` variable) is used on
  purpose: the pinned ref's `azure.py` builds batch-job substitutions across two
  dicts that do not provide that key, so a `${...}` placeholder would render
  unsubstituted and yield an invalid integer. Build-time override via
  `ELB_JOB_TTL_SECONDS` (digits, seconds; default 1800), wired as a build `ARG`
  in `terminal/Dockerfile` and `terminal/Dockerfile.base` and passed into the
  patch step. `scripts/dev/terminal-base-image.sh` passes it as `--build-arg` to
  the base build AND folds it into the base toolchain tag hash, so
  `ELB_JOB_TTL_SECONDS=<n> quick-deploy.sh terminal --rebuild-terminal-base`
  re-tags and rebuilds the base with the override instead of reusing a cached
  base (verified: default tag != override tag). Idempotent; raises on missing
  missing `backoffLimit` anchor. The TTL governs GC only AFTER a terminal state,
  so the finalizer's `backoffLimit: 0` (not safely retryable) is preserved.
  Wired into `main()` after `patch_aks_workload_tolerations`.
- The warmup (`warm-*`) / init-ssd Jobs back the node-local DB cache and are
  managed by the dashboard warmup reconciler (which relies on the Job objects
  existing), so they are intentionally untouched.
- No api/worker, Bicep, or frontend change.

## Validation evidence

- `uv run ruff check terminal/patch_elastic_blast.py api/tests/test_terminal_patch_elastic_blast.py` — clean.
- `uv run pytest -q api/tests/test_terminal_patch_elastic_blast.py` — 25 passed
  (4 new: default TTL, env override, non-numeric override fallback, idempotency).
- Real-template integration: applied `patch_aks_job_ttl` to the actual
  `f4b8b734` templates and parsed the YAML — all four (three batch + finalizer)
  carry `spec.ttlSecondsAfterFinished == 1800` at the correct Job.spec level,
  and the finalizer's `backoffLimit: 0` is preserved.
- One-shot cleanup of the existing backlog: bulk-deleted the completed
  `blastn-batch-*` (2,260) and `elb-finalizer-*` (226) Jobs on the live cluster
  (active/running and warmup Jobs excluded; both counts verified back to 0).
