---
title: prepare-db AKS Job uses per-index backoff so transient blips stop nuking the whole download
description: Switch the prepare-db Indexed Job from a global backoffLimit to backoffLimitPerIndex + maxFailedIndexes so a few transient pod failures no longer fail the entire nt/core_nt download.
tags:
  - blast
  - infra
---

## Motivation

`nt` (and `core_nt`) downloads via the AKS-fanout `prepare-db` mode kept ending
in the dashboard partial state:

```
nt: Job pods succeeded=0/10 failed=12; blobs success=4866/4874 failed=0 pending=0
```

The user re-clicked **Get / Update** more than ten times; every attempt streamed
nearly all blobs and then collapsed to `succeeded=0/10`.

### Root cause (confirmed against the live cluster + Kubernetes docs)

The Indexed Job (`completionMode: Indexed`, 10 shards) set **only** the global
`backoffLimit: 2`. Per the Kubernetes Job semantics, `backoffLimit` is a **total**
pod-failure budget across *all* indexes:

> The number of Pods with `.status.phase = "Failed"` … If either of the
> calculations reaches the `.spec.backoffLimit`, the Job is considered failed …
> once `.spec.backoffLimit` has been reached the Job will be marked as failed and
> any running Pods will be terminated.

So as soon as **3 pod failures accumulated across any of the 10 shards** — a
single OOM / SNAT reset / NCBI 5xx / node preemption per shard over a multi-hour
download is more than enough — Kubernetes marked the whole Job `Failed` and
**killed every still-running pod**, discarding their nearly-complete work. Those
terminated pods also count as failed, which is why the message read
`failed=12` (≈ 2 that tripped the budget + ~10 killed in flight) and
`succeeded=0`. The almost-complete `4866/4874` blob count was just the in-flight
files of the killed pods — not an independent failure. The next retry
re-downloaded everything (`--overwrite=true`) and died the same way: an
unconverging loop.

Live verification (moonchoi sub, `elb-cluster-02`, Kubernetes `v1.34.7`): a fresh
retry Job was running healthy (shard 0 azcopy: 486 transfers, **0 failed**, 20 %
at ~5 min), proving the azcopy mechanics are sound — only the Job orchestration
was broken.

## User-facing change

`nt` / `core_nt` (and any large multi-shard) downloads now tolerate transient
per-shard failures. A blip in one shard retries **only that shard** and no longer
kills the other nine, so the download converges instead of looping on
`succeeded=0/10`.

## API / IaC diff summary

`api/services/k8s/prepare_db_jobs.py` — `build_prepare_db_job_manifest` Job spec:

* **Removed** the global `backoffLimit` (when `backoffLimitPerIndex` is set,
  Kubernetes defaults the global limit to `MaxInt32`, so it no longer acts as a
  cross-shard guillotine).
* **Added** `backoffLimitPerIndex: <backoff_limit>` — per-shard retry budget
  (stable since Kubernetes 1.33; the cluster is 1.34). The existing
  `backoff_limit` parameter now drives this field.
* **Added** `maxFailedIndexes: <shard_count>` — keeps every healthy shard running
  to completion even if a broken shard exhausts its retries; the Job is still
  marked `Failed` at the end if any index ultimately failed (correctly surfaced
  as a partial by the Celery task), but the succeeded shards persist their blobs.

`restartPolicy: Never` (required by the feature) was already set. The
Job-completion detection in `api/tasks/storage/prepare_db_via_aks.py`
(`_job_is_terminal`) already keys off the `Complete` / `Failed` condition *type*,
which covers the new `FailedIndexes` reason — no change needed.

## Validation evidence

* `uv run pytest -q api/tests/test_prepare_db_aks_manifest.py
  api/tests/test_prepare_db_aks_planner.py api/tests/test_prepare_db_aks_task.py`
  → 48 passed. `test_manifest_safety_fields` now asserts `backoffLimit` is absent
  and `backoffLimitPerIndex` / `maxFailedIndexes` are present and bounded.
* `uv run pytest -q api/tests -k "prepare_db or k8s or storage or blast_gate or aks"`
  → 629 passed.
* `uv run ruff check` (changed files) → clean.
* Full suite `uv run pytest -q api/tests` → 2872 passed, 3 skipped.

## Deployment note

The change is in the baked pod-Job manifest builder, so it only takes effect for
**newly dispatched** Jobs after an `api` + `worker` image rebuild
(`scripts/dev/quick-deploy.sh api` with the moonchoi MSAL overrides). A Job
already running was submitted with the old manifest and cannot be patched
(backoff fields are immutable) — let it finish, then redeploy before the next
dispatch.
