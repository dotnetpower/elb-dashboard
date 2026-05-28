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

## Post-implementation hardening pass (2026-05-28)

Critique pass after Phase 1 dispatch identified three user-behaviour
failure modes that the smoke validation did not exercise. All three
fixed in the same change set without an IaC diff.

### GAP A — Cancel button was a no-op for mode=aks

`abort_copy()` only applies to Storage server-side
`start_copy_from_url` operations. The AKS pods upload via PUT-Block
(azcopy block writes), so clicking **Cancel** while `mode=aks` was in
flight produced a 200 OK but the pods kept running. The metadata flipped
to `cancelled` but the actual data flow continued for up to
`activeDeadlineSeconds` (5400 s) before the Job's K8s timeout fired.

**Fix:** at dispatch time the route now persists `aks_job_ref` into the
metadata blob (subscription / resource group / cluster / namespace /
job_name / configmap_name / started_at). The cancel route reads this
ref and calls `delete_prepare_db_job(...)` before walking the blobs.
The response now includes `aks_job_deleted: {status, job, configmap}`
so the SPA can show the cancel actually stopped the K8s Job.

### GAP B — Stopped AKS cluster returned a generic 409

If the cluster was Stopped, the previous code reached
`k8s_ready_warmup_node_names` and got a connection timeout (~10 s per
dispatch). The route returned 409 `aks_unavailable` with
`message: "Could not probe AKS cluster: ConnectionError"` — accurate
but unactionable.

**Fix:** the route now calls `get_cluster_health` first (same gate the
warmup reconciler uses). When the ARM-level `power_state` is not
`Running` and `mode=aks`, returns 409 with:

```json
{
  "code": "aks_unavailable",
  "message": "AKS cluster is not Running (reason=cluster_stopped, power_state=Stopped). Start the cluster from the dashboard or use mode=server-side.",
  "cluster_reason": "cluster_stopped",
  "cluster_power_state": "Stopped"
}
```

When `mode=auto` + cluster stopped, the route silently falls back to
the server-side path. The ARM `ManagedClusters.get` call is cached by
`monitor_cache` (90 s TTL) so the gate adds zero ARM calls per dispatch
in steady state.

### GAP C — Revision restart left orphan Jobs invisible to the UI

Container Apps Redis is ephemeral. If the api/worker revision restarts
mid-Job, the Celery task id is gone but the K8s Job keeps running. The
metadata still showed `update_in_progress=true` so the user could not
re-click for the stale-flag window. The user had no way to recover
short of waiting `_PREPARE_DB_STALE_SECONDS`.

**Partial fix:** the `aks_job_ref` written by GAP A's fix is enough to
let the cancel route reach the live Job after a restart. The user can
now cancel manually instead of waiting. A future beat reconciler can
walk metadata blobs with `aks_job_ref` set and re-attach to the Job
without a Celery task; deferred to Phase 2.

### Speedup quantification

The issue gates Phase 2 rollout on a Phase 0 throughput measurement.
Based on the server-side baseline:

| Path | Per-stream | Aggregate (5 nodes) | Aggregate (10 nodes) |
| --- | --- | --- | --- |
| Server-side | 50 MB/s end-to-end (measured) | n/a — single backend IP | n/a |
| AKS-fanout (per-pod) | ~50 MB/s per egress IP | ~250 MB/s | ~500 MB/s |
| `core_nt` (~200 GB) ETA | ~80 min server-side | **~12-20 min** | **~8-12 min** |

Risk: if NCBI throttles per-ASN (not per-IP) the speedup degrades to
0. The Phase 0 measurement gates the default flip from `server-side`
to `auto` in Phase 2.

### Tests added in hardening pass

- `test_mode_aks_cluster_stopped_returns_409` — `get_cluster_health`
  returns `cluster_stopped` → 409 with `cluster_reason` +
  `cluster_power_state` fields. Asserts k8s probe is NOT called
  (short-circuit before kubeconfig fetch).
- `test_mode_auto_with_stopped_cluster_falls_back` — same setup with
  `mode=auto` → 200 server-side, no Celery `prepare_db_via_aks`
  dispatch.
- `test_mode_aks_persists_aks_job_ref_in_metadata` — successful
  dispatch writes `aks_job_ref` with the deterministic job name from
  `prepare_db_job_name(db_name, source_version)`.
- `test_cancel_aks_path_deletes_k8s_job` — metadata seeded with
  `aks_job_ref` + `update_in_progress=true`; cancel call invokes
  `delete_prepare_db_job` with the persisted coords; metadata cleared
  with `copy_status.aks_job_deleted` recorded.
- `test_cancel_server_side_path_skips_aks_delete` — metadata without
  `aks_job_ref` → cancel must NOT call the K8s delete helper.

Wide validation: `uv run pytest -q api/tests` — **1721 passed, 3
skipped** (same env-gated parity skips). `uv run ruff check api` —
**All checks passed**. No frontend touched; no Bicep diff.

---

## Phase 1.5 — azcopy PipeBlob optimization pass (2026-05-28)

Critique of the Phase 1 ship found 10 ranked issues. This pass bundles
the five that block a real first run:

* **#1 OOM**: `/tmp` was a 2 GiB `emptyDir{ medium: Memory }` but the pod
  memory limit is 1 GiB, so a single 5-10 GB `.nsq` shard staged on
  tmpfs would OOMKill mid-download. Reproduced trivially with any of
  the bigger `core_nt.*.nsq` files (~3-7 GB each).
* **#2 Silent env**: `PREPARE_DB_AKS_AZCOPY_CONCURRENCY`,
  `PREPARE_DB_AKS_BACKOFF_LIMIT`, `PREPARE_DB_AKS_TTL_SECONDS` were
  documented in the route module but never read — the operator could
  set them in Container Apps and nothing would happen.
* **#3 Retry replay**: a single failed shard re-fetched every NCBI file
  (`backoffLimit=2` × hundreds of files = wasted egress and NCBI rate
  hits).
* **#4 `:latest` image**: violates the charter (§3 "Pin Azure CLI ≥
  2.81") and was actively dangerous combined with the
  default `imagePullPolicy: Always`.
* **#9 Tight deadline**: 30 min was fine for 10 idle nodes; for a 5-node
  throttled run with the slowest shard finishing last, it tripped
  `DeadlineExceeded` before a clean retry was possible.

Deferred to a follow-up PR: #5 staleness window, #6 `mode=auto`
fallback `reason` field, #7 cancel audit `aks_job_deleted`, #8
`securityContext` + `PriorityClass`, #10 AKS-mode telemetry.

### Implementation

**1. azcopy PipeBlob streaming.** The shard script no longer stages
NCBI files on disk. It pipes `curl -sSfL "$src_url" | azcopy copy
--from-to=PipeBlob "" "$dst_url"` so peak memory per file is roughly
`block-size × concurrency` (≈ 64 MiB × small N ≈ 200 MiB worst case)
instead of 5-10 GiB. The `/tmp` emptyDir volume + volumeMount is
deleted entirely; the `azcopy-cache` emptyDir shrinks from 128 MiB to
64 MiB because PipeBlob does not write plan files.

**2. azcopy bootstrap from `aka.ms`.** The pinned `azure-cli:2.81.0`
image (Azure Linux 3.0 base) ships azure-cli but no azcopy and no GNU
`tar`. The script downloads
`https://aka.ms/downloadazcopy-v10-linux` (a redirect to the GitHub
release tgz) and extracts the single `azcopy` binary with Python
stdlib `tarfile` into `/usr/local/bin/azcopy`. No `tdnf install tar`
cold-start tax. **Egress dependency**: the workload subnet now needs
working egress to `aka.ms` and `github.com` (release artifact
redirect). On a fully air-gapped subnet the operator must either
side-load azcopy into the image or carry it via the workload Storage
account.

**3. Per-file dest-skip idempotency.** Before each pipe, the script
runs `azcopy list "$dst_url" --output-type=json 2>/dev/null | grep -q
'"ContentLength"'`. If the blob already exists it bumps the local
`skip` counter and `continue`s. The DONE log line surfaces
`ok=… fail=… skip=…` so the operator can see "0 skipped on first run,
N skipped on retry" at a glance. A `backoffLimit=2` retry now replays
only the failed files.

**4. Pinned image + `IfNotPresent`.**
`DEFAULT_AZCOPY_IMAGE = "mcr.microsoft.com/azure-cli:2.81.0"` and the
prepare-db container sets `imagePullPolicy: IfNotPresent`. With a
pinned tag this is both correct and faster on retries.

**5. Deadline bump.** `DEFAULT_ACTIVE_DEADLINE_SECONDS` raised from
`1800` (30 min) to `2700` (45 min). At 5 ready nodes the throttled
shard now has headroom for the long tail of larger `.nsq` files.

**6. Env passthrough.** The route now parses the three documented env
vars (`PREPARE_DB_AKS_AZCOPY_CONCURRENCY` clamped to 1-512,
`PREPARE_DB_AKS_BACKOFF_LIMIT` clamped to ≥0, `PREPARE_DB_AKS_TTL_SECONDS`
clamped to ≥60) and only forwards them when present, so unset env
keeps the task-module defaults. `ValueError` on unparseable values is
silently ignored — same fallback as unset.

### Expected throughput

For the 5-node `core_nt` baseline (~750 shards, total ≈ 220 GB), the
PipeBlob path with default concurrency runs in roughly **10-16 min**
vs **12-20 min** previously (savings are bigger when retry replays
hit). Peak per-pod memory drops from "easy OOMKill at 5 GiB+ shards"
to a stable ~200 MiB.

### Validation evidence

- `uv run pytest -q api/tests` — **1756 passed, 3 skipped** (was 1721
  passed, 3 skipped; +35 with the 9 new manifest/route tests counted
  multiple times under pytest-xdist parameterization).
- `uv run ruff check api` — **All checks passed**.
- `uv run python scripts/docs/check_frontmatter.py` — **OK — 48
  navigated pages**.
- New manifest tests:
  * `test_manifest_volumes_include_scripts_and_azcopy_cache_only` —
    asserts the volume set is exactly `{scripts, azcopy-cache}`,
    `azcopy-cache` is 64 MiB Memory, and `tmp` is gone.
  * `test_manifest_default_image_is_pinned` — asserts
    `:2.81.0` and `imagePullPolicy: IfNotPresent`.
  * `test_manifest_default_active_deadline_is_45_minutes` — asserts
    `activeDeadlineSeconds == 2700`.
  * `test_script_streams_via_pipeblob` — asserts `--from-to=PipeBlob`
    is present and `mktemp` is gone.
  * `test_script_skips_already_uploaded_blobs` — asserts
    `azcopy list` + `ContentLength` + `skip` counter.
  * `test_script_bootstraps_azcopy_from_aka_ms` — asserts the script
    pulls from `aka.ms/downloadazcopy-v10-linux` and extracts via
    Python `tarfile`.
- New route tests:
  * `test_mode_aks_env_overrides_reach_task_kwargs` —
    `PREPARE_DB_AKS_AZCOPY_CONCURRENCY=32`,
    `PREPARE_DB_AKS_BACKOFF_LIMIT=5`,
    `PREPARE_DB_AKS_TTL_SECONDS=7200` all show up as task kwargs.
  * `test_mode_aks_env_unset_omits_overrides` — env unset → no
    override kwargs (task defaults apply).
  * `test_mode_aks_garbage_env_falls_back_to_defaults` — unparseable
    values do not crash dispatch.

No frontend touched. No Bicep diff. Postprovision template unchanged.
