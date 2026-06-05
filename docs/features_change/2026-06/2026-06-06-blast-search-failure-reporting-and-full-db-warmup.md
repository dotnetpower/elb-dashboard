---
title: BLAST search failure reporting + full-DB warmup coverage
description: A K8s-stage BLAST search failure now reports at the "BLAST Run" step with the real cluster-side error, and single-shard (full) databases are warmed on every node so an unsharded search no longer fails on an un-warmed node.
tags:
  - blast
  - user-guide
---

# 2026-06-06 — BLAST search failure reporting + full-DB warmup coverage

## Motivation

A dashboard-driven 16S blastn run (`47e2c79c-…-cf873ae3ea47`) failed but the
"Run details" page reported **"Job Failed at Submit Job"** with the banner text
`[parallel-prep] running 4 azcopy checks concurrently` — a benign helper log
line. Live investigation showed three distinct defects:

1. **Wrong failed step + hidden error (reporting).** The `submit` Celery task
   *succeeded* (phase `submitted`); the job later flipped to `failed` at the
   K8s **running** stage when `poll_running_status` observed the blastn pod
   `BackoffLimitExceeded`. The K8s-refresh path wrote the failure against a
   bogus `failed` progress step (not a real timeline step) with no error
   detail, so the SPA defaulted to "Submit Job" and surfaced the last benign
   helper log line instead of the real cause.

2. **No cluster-side diagnostics (observability).** The blastn container exited
   with code 2 after ~0.6 s (`BLAST_RUNTIME-000.out`: `run exitCode 000 2`),
   but the pod was deleted, the cluster had no Container Insights, and no
   App Insights telemetry was flowing — so the real error was invisible from
   the dashboard.

3. **Full DB warmed on only one node (root cause).** A read-only per-node probe
   proved the 16S database was staged on **1 of 10** blast-pool nodes. The
   dashboard warms a single-shard (full) database as **one** Job pinned to
   node 0 (`warm-<db>-00`), but the search batch schedules onto **any**
   `workload=blast` node. ~90 % of the time the batch lands on a node without
   the DB → blastn exits 2 (`BLAST Database error: No alias or index file
   found`). Sharded DBs (e.g. `core_nt`, 10 shards) were unaffected because
   each node already gets its own shard and the sharded batch is pinned.

## User-facing change

* A BLAST search that fails on the cluster now reports **"Job Failed at
  BLAST Run"** (not "Submit Job"), with prior steps shown as completed and the
  timeline no longer spinning on an earlier step.
* The failure banner shows the **real cluster-side error** — the captured
  blastn stderr (`metadata/FAILURE.txt`) when present, otherwise a concise
  `BLAST search exited with code N on the cluster (… the database may not be
  staged on the assigned node …)` message derived from `BLAST_RUNTIME-NNN.out`.
* A single-shard (full) database is now **warmed on every Ready node**, so an
  unsharded search succeeds regardless of which node the batch lands on.

## API / IaC diff summary

No HTTP route, Bicep, or Celery task name changed.

### `api/services/blast/job_state.py`

* New `_read_blast_runtime_failure(storage_account, job_id)` — best-effort read
  of the cluster-side failure artifacts (`metadata/FAILURE.txt`,
  `logs/BLAST_RUNTIME-NNN.out`) returning a concise one-line message.
* `_payload_with_refresh_progress(..., failed_step_key=, error_detail=)` — on a
  terminal `failed` refresh, records the failure against the real execution
  step (`running` / `exporting_results`) with `success=False` + `error`,
  completes the prior steps, and sets top-level `failed_step` / `error`.
* `_refresh_running_blast_state` — on `k8s_status == "failed"` it now resolves
  the failed step from the prior phase, reads the runtime failure detail, sets
  `error_code="blast_search_failed"`, and records the detail in history.
* `_local_to_blast_job` — surfaces payload `failed_step` / `error` on the
  serialized `output` object so the SPA banner resolves the correct step and
  error.

### `api/services/warmup/jobs.py`

* `build_warmup_job_plan` — a single-shard DB (`num_shards == 1`) on a
  multi-node cluster is now **broadcast**: one warmup Job per Ready node, all
  staging the same shard-00 (full DB) content, with per-node tracking ordinals
  so Job names stay unique (`warm-<db>-00 … warm-<db>-NN`) and the status
  aggregation counts each node. Multi-shard DBs keep one-shard-per-node
  placement. `_build_job` gained `db_content_shard_idx` to decouple the
  tracking ordinal (name/label/node) from the DB content shard.

### `web/src/components/BlastStepTimeline/predicates.ts`

No code change required — the existing `inferFailedStepKey` reverse-scan now
resolves to `running` because the backend marks that step `success=False`. A
regression test locks the behaviour.

## Validation evidence

* Live root-cause proof: a read-only `DaemonSet` listed `/workspace/blast` on
  every blast-pool node — `16S_ribosomal_RNA.*` present on `…vmss00004g` only,
  absent from the other 9 nodes.
* `uv run pytest -q api/tests` — **2920 passed, 3 skipped**. New tests:
  `test_refresh_running_blast_state_failure_marks_running_step_with_detail`,
  `test_refresh_running_blast_state_failure_falls_back_to_generic_detail`,
  `test_read_blast_runtime_failure_*` (3),
  `test_single_shard_db_is_broadcast_to_every_node`,
  `test_single_shard_single_node_keeps_one_job`.
* `cd web && npm run build` clean; `npm test -- --run predicates …` — 154
  passed, including `inferFailedStepKey maps a K8s-stage failure to the
  BLAST Run step, not Submit Job`.
* `uv run ruff check` clean on all touched files.

## Follow-up (recommended live verification after deploy)

The warmup-plan change runs in the `worker` sidecar (baked image), so a worker
redeploy is required to take effect. After deploying, re-warm 16S and re-run the
per-node probe to confirm the DB lands on every Ready node, then submit a 16S
search and confirm it completes. A separate gap remains: **auto-warmup does not
re-cover nodes added by autoscale-up after the initial warmup** (existing
`_mark_stale_warmup_nodes` handles node *replacement*, not *addition*) — tracked
for a follow-up reconcile change.
