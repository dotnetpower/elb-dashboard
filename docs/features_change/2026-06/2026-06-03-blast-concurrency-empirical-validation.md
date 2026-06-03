---
title: BLAST concurrency — live empirical validation + capacity-gate harness
description: Measured how many BLAST searches the live AKS cluster actually runs
  in parallel via a new reusable harness, and folded the findings back into the
  capacity-gate design (DB-memory hard reject + per-DB slot key).
tags:
  - blast
  - research
  - testing
---

# BLAST concurrency — live empirical validation (2026-06-03)

## Motivation

The capacity-gate design ([docs/research/aks-capacity-gate.md](../../research/aks-capacity-gate.md))
was written from first principles and assumed the binding constraint on
parallel BLAST submits was node CPU/memory **request pressure**. Before
tuning the gate's slot count for "smart queuing", we wanted real numbers:
how many `/v1/jobs` searches does the live cluster actually run at once, does
queueing hold the rest back, and what fails under a burst.

## User-facing change

No runtime behaviour change for end users in this commit. This is a
measurement + documentation pass plus a reusable test tool:

- **New harness** under [scripts/e2e/concurrency/](../../../scripts/e2e/concurrency/):
  - `harness.py` — submits N Mode-B inline-FASTA jobs (reusing the SPA's New
    Search templates) and polls each job's status into a machine-readable
    timeline.
  - `watch_pods.sh` — samples `app=blast` pods (the real shard label) to record
    authoritative pod-level concurrency (`running_pods`, `distinct_jobs`,
    `running_jobs`).
  - `extract_queries.py` — parses `web/src/pages/blastSubmit/queryExamples.ts`
    so tests stay in sync with the UI's example FASTA.
  - `run.sh` — orchestrator: loads the admin token from the cluster, starts the
    watcher, runs a `single` / `sequential` / `burst` scenario, writes results
    under `.logs/e2e/concurrency/<scenario>-<n>-<ts>/`.
- **Research doc updated** with a new §9 "Empirical validation" and a corrected
  §7 non-goal.

## Findings (live `elb-cluster-02`, 10 × `Standard_E16s_v5`)

1. **`core_nt` cannot run on this pool at all** — `elastic-blast` rejects it
   pre-pod: needs ≥ 251.7 GB, node has 128 GB. HTTP `202` is request-accept,
   not job-admit (status then flips to `failed`/`submit_failed`).
2. **Peak concurrent RUNNING jobs under a 10-job burst = 2–3** (authoritative
   3 s pod watcher saw 3; the slower 12 s status poll saw 2). elb-openapi
   serialises admission (~39–46 s to accept 10 submits) and dispatches in small
   batches (~70 s cycle), holding the tail in its own queue. The 160-vCPU node
   ceiling was never the binding constraint — elb-openapi's internal ~2–3
   dispatch concurrency was.
3. **Concurrent same-DB submits race on the node-local `hostPath` DB cache.**
   A 16S burst of 10 finished ≈ **9 failed / ≈ 1 succeeded**, almost all with
   `"No alias or index file found for nucleotide database [16S_ribosomal_RNA]"`
   → `BackoffLimitExceeded`. The same query succeeds every time in isolation
   (≈ 78 s wall). The failure is a staging race, not a query defect.

## Design impact (folded into the gate research doc)

- Promote a **`db_memory_infeasible` non-retryable reject** (predicted job
  memory > max node allocatable) to the next gate increment — this is the
  dominant *hard* failure and the highest-value smartness.
- Make the slot key **per-(cluster, DB)** (`elb:blast:slots:<cluster>:<db>`) so
  two different DBs can run in parallel but two same-DB jobs cannot collide on
  staging.
- Keep `BLAST_GATE_MAX_SLOTS_PER_CLUSTER=1` (Charter §12a Rule 4 default-OFF) —
  raising it before the staging race is fixed just multiplies failures. The
  measurement **validates** the conservative default rather than motivating a
  bump.

## API / IaC diff summary

- No API route, schema, Celery task, or Bicep change.
- `api/services/blast/capacity_gate.py` is unchanged (still Stage 1, not wired
  into live submit) — the findings inform its Stage-2 design, recorded in the
  research doc.
- Added: `scripts/e2e/concurrency/{run.sh,harness.py,watch_pods.sh,extract_queries.py}`.
- Edited: `docs/research/aks-capacity-gate.md` (§7 note, new §9, §10 reference).

## Validation evidence

- `single` 16S baseline: dispatch ≈ 45 s → running ≈ 15 s → `succeeded`,
  ≈ 78 s wall (harness timeline).
- `burst 10` 16S: `submitted ok=10/10 statuses=[202]`; peak pod-level
  `running_jobs=3` (status poll `max_concurrent_running_status=2`); final
  tally **9 `failed` / 1 `succeeded`** with the `No alias or index file found`
  DB-staging error captured from the `blast` container logs. Raw timeline +
  `summary.json` at `.logs/e2e/concurrency/burst-10-20260603-004554/`.
- `uv run ruff check scripts/e2e/concurrency/harness.py extract_queries.py` clean.
- Docs: `uv run python scripts/docs/check_frontmatter.py` +
  `mkdocs build --strict` green.
