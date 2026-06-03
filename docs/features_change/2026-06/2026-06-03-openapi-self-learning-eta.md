---
title: Self-learning per-job ETA for the elb-openapi status API
description: Continuously self-learning run-time model that adds per-job remaining-time, queue position, and start/finish estimates to the elb-openapi status payload.
tags:
  - blast
  - operate
---

# Self-learning per-job ETA for the elb-openapi status API

## Motivation

Users submit ~20 separate OpenAPI BLAST requests at once and poll each job's
status as XML. They asked for (a) remaining time for a running job, (b) queue
position / jobs-ahead for a queued job, and (c) a precise per-job estimate that
**self-learns** from each completed run rather than using a fixed constant.

The result-equivalence guarantee is unchanged: this feature is observability
only. It never touches `num_nodes`, `searchsp`, sharding, or the BLAST command,
so results still equal NCBI web BLAST.

## User-facing change

The `/status` payload gains an optional `eta` object, present only when
`ELB_OPENAPI_ETA_ENABLED` is truthy:

- **Running job**:
  `{ remaining_seconds, estimated_finish_seconds, estimated_finish_at, confidence, basis }`
- **Queued job**:
  `{ jobs_ahead, estimated_start_seconds, estimated_finish_seconds, estimated_start_at, estimated_finish_at, confidence, basis }`

`confidence` is `high` (≥10 samples for the key), `medium` (≥`_MIN_SAMPLES`), or
`low` (fallback estimate). `basis` names the partition key / fallback used.

The estimate is keyed on input features — database, query size bucket
(log-bucketed sequence count + residue count), and cold/warm cluster state — so
each (db, bucket, cold/warm) combination learns its own EWMA mean/variance from
observed run times. The queued estimate runs a C-server (`MAX_ACTIVE_SUBMISSIONS`)
min-heap simulation over the priority-sorted queue.

When the feature is OFF the payload is unchanged except that two harmless
feature fields (`query_seqs`, `query_bases`, both `0`) are captured at submit.

## API / IaC diff summary

- **New** `scripts/dev/openapi-overlays/eta.py` — import-safe, opt-in overlay
  copied into the sibling `docker-openapi` build context as `app/eta.py`. Pure
  Python core; Azure Table backing is optional and lazy.
- **New** `scripts/dev/openapi-overlays/test_eta.py` — 16 unit tests
  (feature parsing, learning convergence, cold/warm separation, fallback chain,
  C=2 queue stagger, cross-replica ETag-merge convergence).
- **Modified** `scripts/dev/patch-openapi-build-context.py` — copies the overlay
  and injects `_eta.enabled()`-gated hooks: import, submit feature capture, a
  completion sample record at the single state-write choke point `_update_job`
  (with an **atomic `eta_recorded` claim** under `_jobs_lock`), and the status
  `eta` projection on **both** status surfaces — `_external_job_payload`
  (`/api/v1/elastic-blast/jobs/{id}`) and the canonical `get_job_status`
  (`/v1/jobs/{id}/status`).
- No infra/Bicep change. No change to the shared production image (`4.18`); a
  separate `elb-openapi:eta-test` tag is used for validation.

### Hardening applied after design critique

- **Atomic completion record** — the `eta_recorded` flag is claimed inside
  `_jobs_lock` before recording, so concurrent status polls of the same
  completed job record the sample exactly once.
- **Cross-replica convergence** — `_Store.update` uses an ETag optimistic-merge
  (re-read row → re-apply EWMA → `update_entity`/`create_entity` with bounded
  `ELB_OPENAPI_ETA_UPDATE_RETRIES` retries) instead of last-writer-wins upsert,
  and `_Store.get` re-reads the Table when the cached row is older than
  `ELB_OPENAPI_ETA_CACHE_TTL_SECONDS`, so two openapi replicas accumulate
  samples instead of clobbering each other.
- **Observability** — `_Store._table` logs once (warn) when ETA is enabled but
  no Table backing is configured or client init fails, and once (info) on
  successful init, so an "ETA never gets more confident" state is diagnosable.

### Endpoint-coverage fixes (found in live validation)

Live validation surfaced that the openapi service exposes **two** status
surfaces and the original hooks only covered one:

- `/v1/jobs/{id}/status` (`get_job_status`) builds its own inline dict and does
  **not** route through `_external_job_payload` (which serves only the external
  facade `/api/v1/elastic-blast/jobs/{id}`). The first burst returned
  `eta=null` on every job because the harness/`status_url` polls the canonical
  endpoint. Fix: project `eta` on the `get_job_status` payload too.
- The completion **sample recorder** had the same single-surface problem, so
  `basis.samples` stayed `0` (no learning) when jobs were observed via the
  canonical endpoint. Fix: move recording into the endpoint-independent choke
  point `_update_job`, which also fires from the background watchdog with zero
  polling, keeping exactly-once semantics via the persisted `eta_recorded` flag.

### Known limitations (documented, not blocking)

- Cold detection (`_job_was_cold`) is an O(n) scan, making `record_sample`
  effectively O(n²) over the job set — acceptable at the 20-burst scale; jobs
  are not garbage-collected (pre-existing).
- Queued jobs have `started_at=None`, so the very first cold job's ETA cannot
  account for cluster spin-up until at least one cold sample is recorded.

## Tunables (all default-safe, opt-in)

`ELB_OPENAPI_ETA_ENABLED`, `ELB_OPENAPI_ETA_BIAS_Z`, `ELB_OPENAPI_ETA_MIN_SAMPLES`,
`ELB_OPENAPI_ETA_EWMA_WINDOW`, `ELB_OPENAPI_ETA_COLD_GAP_SECONDS`,
`ELB_OPENAPI_ETA_DEFAULT_RUN_SECONDS`, `ELB_OPENAPI_ETA_TABLE`,
`ELB_OPENAPI_ETA_TABLE_CONN`, `ELB_OPENAPI_ETA_CACHE_TTL_SECONDS`,
`ELB_OPENAPI_ETA_UPDATE_RETRIES`.

## Validation evidence

- `uv run ruff check scripts/dev/openapi-overlays/ scripts/dev/patch-openapi-build-context.py` → clean.
- `uv run python -m pytest scripts/dev/openapi-overlays/test_eta.py -q` → **16 passed**.
- Fresh-copy patch apply (`cp -a ~/dev/elastic-blast-azure/docker-openapi …` →
  `patch-openapi-build-context.py`) → `py_compile` of `app/main.py` + `app/eta.py`
  OK, all four hook sites grep-confirmed.
- Test image `acrelbdashboard3abp67bppe.azurecr.io/elb-openapi:eta-test` rebuilt
  with the hardened overlay (ACR run `de46`).
- **Live validation on `elb-cluster-02`** (single `eta-test` replica, ETA
  enabled, shared image left at `4.18` and restored afterwards):
  - 5-job burst → `/v1/jobs/{id}/status` returned `eta` for every non-terminal
    job: running jobs carried `remaining_seconds` (38.5 s, 41.0 s); queued jobs
    carried `jobs_ahead` 2/3/4 with **monotone** `estimated_start_seconds`
    28.8 → 35.5 → 138.5 s, confirming the C=2 queue simulation staggers starts
    correctly.
  - Two consecutive 3-job bursts → after the first burst's three completions,
    the second burst's jobs reported `basis.samples=3`, `confidence=medium`
    (up from `low`), and learned `remaining_seconds` (82.0 s, 71.1 s) instead
    of the 110 s default — proving online EWMA learning fires end-to-end.
  - The `eta-test` base image lacks `azure-data-tables`/`azure-identity`, so the
    Table store degrades to the in-memory fallback (warn-once logged as
    designed); learning was validated against that fallback. Cross-replica
    Table persistence is covered by the unit tests.
  - NCBI parity unchanged: ETA never touches the submit/`searchsp`/sharding
    path; 11/11 jobs across the validation bursts that reached submit ran the
    standard pipeline.
