# prepare-db AKS-fanout: in-pod azcopy resume loop (no full-shard re-download)

## Motivation

Large BLAST DB downloads (`nt`, `core_nt`) kept failing on the AKS-fanout
prepare-db path and never converged, surfacing in the dashboard as a
perpetual `partial · N failed` / red error state that "계속 오류가 해결되지
않고" (kept not resolving).

Root cause, confirmed live on 2026-06-05 by re-triggering both failing DBs on
`elb-cluster-02` and watching the pods:

- Each shard streams ~200 GB / ~486 NCBI files over ~20 min. At that scale a
  single transient per-file hiccup (S3 503 / SNAT reset / read timeout) is
  near-certain, and azcopy reports the whole run as `exit 1`
  (`CompletedWithErrors`) even when 485/486 files committed fine
  (observed: `Number of File Transfers Failed: 1`, `azcopy_exit=1`).
- The pod script ran a single `azcopy copy --overwrite=true` and `exit $rc`.
  The non-zero exit failed the pod, so the Job's `backoffLimitPerIndex`
  relaunched a **fresh pod that re-downloaded the entire shard from scratch**
  (`--overwrite=true` re-fetches every file). The retry then hit another
  single-file blip near the end and failed the same way — never converging
  within the per-shard retry budget → Job `Failed` → orphaned → `partial`.
- The small `16S_ribosomal_RNA` DB (18 MB, 15 files) was too small to trip a
  transient failure, so it always succeeded on the first attempt — which is
  why only the large DBs were stuck.

## User-facing change

Large DB downloads now converge reliably instead of looping. A transient
per-file failure no longer throws away ~200 GB of completed work.

## Code / behaviour change

`api/services/k8s/prepare_db_jobs.py` — `PREPARE_DB_AKS_SCRIPT` now wraps the
`azcopy copy --from-to=S3Blob` call in a bounded **in-pod resume loop**:

- Attempt 1 uses `--overwrite=true` (heals any truncated/legacy blob).
- Retries (up to `ELB_AZCOPY_MAX_ATTEMPTS`, default 5) use
  `--overwrite=ifSourceNewer`, so the already-committed blobs (dest LMT newer
  than the source snapshot) are **skipped** and only the handful of failed
  files are re-fetched — converging in seconds, not another full download.
- The pod exits non-zero only after all in-pod attempts fail, preserving the
  existing Job semantics (`backoffLimitPerIndex` relaunch, honest `partial`
  for a genuinely unreachable file).

No manifest, RBAC, network, or Storage-posture change. Storage stays
`publicNetworkAccess: Disabled`; azcopy still authenticates with the kubelet
MI through the private endpoint.

## Validation

- `uv run ruff check api/services/k8s/prepare_db_jobs.py api/tests/test_prepare_db_aks_manifest.py` — clean.
- `uv run pytest -q api/tests/test_prepare_db_aks_manifest.py api/tests/test_prepare_db_aks_planner.py api/tests/test_prepare_db_aks_task.py api/tests/test_orphan_prepare_db_reconcile.py` — 67 passed.
- New regression test `test_script_resumes_in_pod_instead_of_full_redownload`
  locks the resume loop (`ELB_AZCOPY_MAX_ATTEMPTS`, `ifSourceNewer` retry,
  `while` budget, `exit 0` short-circuit). Updated
  `test_script_overwrites_to_heal_partial_blobs` for the new
  `--overwrite="$overwrite"` form.
- Live evidence (cluster-02, Storage stayed Disabled the whole time):
  `16S_ribosomal_RNA` re-download completed in ~50 s, `ready=true`,
  `source_version` promoted. `nt` re-download confirmed the failure mode:
  shards reached ~85-90% then `CompletedWithErrors (1 failed)` → fresh pod
  restart from ~3% — exactly the non-convergence this fix removes.

## Deploy note

The change is a baked pod-script string, so it requires an `api`+`worker`
image rebuild to persist (charter §13 redeploy exception: pod-script bug,
not reproducible in Tier 1 pytest / Tier 2a uvicorn). After deploy,
re-dispatch the affected DBs (`nt`, `core_nt`) so a fresh Job picks up the
new script.
