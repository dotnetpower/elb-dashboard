---
title: "prepare-db: optional AKS-fanout download mode (Phase 1)"
description: "Add `mode=server-side | aks | auto` to /api/storage/prepare-db so an idle AKS cluster can pull NCBI shards in parallel for the 5-10x speedup."
tags:
  - blast
  - infra
  - release
---

# prepare-db: optional AKS-fanout download mode (Phase 1)

GitHub issue: [#7](https://github.com/dotnetpower/elb-dashboard/issues/7)

## Motivation

The current Prepare DB pipeline triggers Azure server-side copy (Storage
back-end pulls from NCBI's S3) for every shard sequentially. For
`core_nt` (~250 files, ~1.2 TB) this takes ~80 min because NCBI sees a
single source IP and throttles aggressively. When the workload AKS
cluster is otherwise idle, we can parallelise per-node — each node has a
distinct egress NAT IP from NCBI's view — and finish in ~30 min (5-10x).

The change is **opt-in** for Phase 1: a new `mode` request body field
that defaults to env-default `server-side`, so all existing callers keep
the unchanged behaviour byte-for-byte.

## User-facing change

`/api/storage/prepare-db` accepts a new optional body field:

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `mode` | `"server-side" \| "aks" \| "auto"` | `PREPARE_DB_AKS_MODE_DEFAULT` env (falls back to `"server-side"`) | Selects copy strategy. |
| `aks_resource_group` | `string` | optional | Required when `mode in {"aks", "auto"}`. |
| `cluster_name` | `string` | optional | Required when `mode in {"aks", "auto"}`. |

Behaviour matrix:

- `mode="server-side"` (default) — unchanged. Per-file
  `start_copy_from_url` against NCBI S3, polled by `_poll_copy_completion`.
- `mode="aks"` — dispatches an Indexed K8s `Job` (`max_pods` parallel
  pods × `files_per_pod` shards each) that runs `azcopy login --identity`
  + `curl <S3-URL> | azcopy copy` per shard. Returns 409
  `{"code": "aks_unavailable", "message": "...", "ready_nodes": N,
  "required_nodes": M}` when AKS coords are missing or `ready_nodes <
  PREPARE_DB_AKS_MIN_IDLE_NODES` (default 3). **Never silently falls back
  to server-side.**
- `mode="auto"` — try `aks` first; if AKS is unavailable, fall through to
  the server-side path.

Response on the AKS path adds `mode: "aks"` and `ready_nodes` plus the
standard async fields (`task_id`, `statusQueryGetUri`).

Concurrency invariants unchanged:

- Metadata `update_in_progress=true` + the stale-flag recovery window
  remain the cross-process gate. Re-clicking Prepare DB while a Job is
  in flight returns the existing 409 `update_in_progress` (the Job is
  not duplicated).
- On partial completion the metadata blob lands in the same shape the
  server-side path uses: `update_in_progress=false`,
  `copy_status.phase="partial"`, `failed_files=[…]`, so the user can
  re-click to resume.
- K8s `ttlSecondsAfterFinished: 3600` guarantees no zombie pods 1 h
  after Job completion.

## API / IaC diff summary

- New route helper `_try_dispatch_aks_mode` in
  `api/routes/storage/prepare_db.py` — wraps the AKS path; raises
  `HTTPException(409, …)` when AKS is unavailable, returns `None` for
  `mode=auto` fallback.
- New body field parsing in the `prepare_db` route — runs before
  Storage validation; rejects unknown modes with 400.
- New helper `_list_keys_with_sizes` in `api/routes/storage/common.py`
  — sibling to `_list_keys`; reads `<s3:Size>` from the NCBI snapshot
  XML so the LPT planner can balance shards by total bytes. Cached
  separately (64-entry LRU) and respects the same circuit breaker.
- New Celery task `api.tasks.storage.prepare_db_via_aks` — routes via
  the existing `task_routes={"api.tasks.storage.*": {"queue": "storage"}}`
  wildcard; submits Job, polls
  `.status.{active,succeeded,failed,completions,conditions}`, runs the
  same `_poll_copy_completion` the server-side path uses, and on
  success runs the standard `_promote_success` mutator (shard set
  upload + signature ETag refresh + `db_order_oracle` staleness flag).
- K8s manifest builder `api/services/k8s/prepare_db_jobs.py` (already
  in tree) — pre-existing helpers, no signature changes.
- **No IaC diff.** No Bicep, no new Storage RBAC. The kubelet identity
  already has `Storage Blob Data Contributor` from the warmup grant; the
  shared MI already has the K8s ARM permissions used by the warmup
  task module.

New environment variables (all optional, all have safe defaults):

| Variable | Default | Purpose |
| --- | --- | --- |
| `PREPARE_DB_AKS_MODE_DEFAULT` | `server-side` | Default `mode` value. |
| `PREPARE_DB_AKS_MIN_IDLE_NODES` | `3` | Minimum ready warmup nodes for the AKS path to dispatch. |
| `PREPARE_DB_AKS_NAMESPACE` | `default` | Namespace for the Job + ConfigMap. |
| `PREPARE_DB_AKS_IMAGE` | `mcr.microsoft.com/azure-cli:2.81.0` | Pod image (azcopy + curl). |
| `PREPARE_DB_AKS_MAX_PODS` | `5` | Max parallel pods. |
| `PREPARE_DB_AKS_FILES_PER_POD` | `8` | Approximate shard size (refined by LPT planner). |
| `PREPARE_DB_AKS_AZCOPY_CONCURRENCY` | `8` | `azcopy --concurrency-value` per pod. |
| `PREPARE_DB_AKS_BACKOFF_LIMIT` | `2` | K8s `backoffLimit`. |
| `PREPARE_DB_AKS_TTL_SECONDS` | `3600` | `ttlSecondsAfterFinished` (1h zombie-pod cleanup). |
| `PREPARE_DB_AKS_ACTIVE_DEADLINE_SECONDS` | `5400` | K8s Job hard timeout. |

## Validation evidence

Backend test suites added in this change:

- `api/tests/test_prepare_db_aks_planner.py` — 12/12 green. Covers LPT
  shard balancing (size-aware), max-pods cap, empty input, deterministic
  ordering, 10 GB-file isolation, unknown-sizes fallback.
- `api/tests/test_prepare_db_aks_manifest.py` — 17/17 green. Covers
  deterministic job name (≤52 chars, K8s-safe), Indexed completion,
  TTL=3600, downward-API env, `restartPolicy=Never`, `workload=blast`
  toleration, ConfigMap shape, scripts volume mode 0o755, tmp tmpfs.
- `api/tests/test_prepare_db_aks_route.py` — 9/9 green. Covers
  `mode=server-side` (unchanged), `mode=aks` (dispatches Celery task
  with `file_sizes` + queue=storage), `mode=aks` missing coords (400),
  `mode=aks` insufficient idle nodes (409 `aks_unavailable`), concurrent
  in-flight (409 `update_in_progress`), `mode=auto` AKS-then-fallback,
  `mode=auto` AKS-when-available, invalid mode (400).
- `api/tests/test_prepare_db_aks_task.py` — 7/7 green. Covers happy
  path → promote shape (`copy_status.mode=aks`,
  `copy_status.phase=completed`, `sharded=true`, `signature_etag`),
  submit error → partial w/ `aks_submit_summary`, blob partial → partial
  w/ `failed_files`, always-delete Job/ConfigMap, missing-Job recovery,
  validation errors.

Pre-existing regression suites still green:

- `api/tests/test_prepare_db_hardening.py`, `api/tests/test_prepare_db_routes.py`
  — 9/9 green (no behaviour change to default path).

Full backend sweep: `uv run pytest -q api/tests` — **1633 passed, 3 skipped**
(skipped are environment-gated parity tests, unchanged).

Lint: `uv run ruff check api` — **All checks passed**.

Infra: no `azd provision --preview` run because there is no Bicep diff
in this change.

Frontend: untouched. The existing `prepareBlastDb` typed client in
`web/src/api/monitoring.ts` does not pass `mode`, so the default
(`server-side`) is used and the unchanged behaviour ships.

## Out of scope (deferred to Phase 2+)

- UI control to choose `mode` from the Prepare DB modal (Phase 1 is
  backend-only; the field is reachable today only via the OpenAPI
  / curl path).
- Live progress streaming from individual pods (current snapshot writes
  `copy_status` per poll).
- Per-pod log-tailing into the SPA terminal.
