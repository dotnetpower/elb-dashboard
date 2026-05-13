# 2026-05-14 — Job submit & warmup hardening

## Motivation

Two production failures observed during AKS cluster bring-up + first job submit:

1. **Warmup DaemonSet hit `Init:CrashLoopBackOff` with `azcopy 403
   AuthorizationFailure`.** Root cause: AKS kubelet UAMI was granted Storage
   Blob Data Contributor by `assign_aks_roles_activity` immediately after
   cluster creation, but the AAD-side propagation can take 60–180 s. The
   DaemonSet pod tries azcopy before the role is effective, fails, then sits
   in CrashLoopBackOff until k8s back-off (5 min cap) finally lets it retry.
   The orchestrator's poll then declares `failed` while the role would have
   been ready 30 s later. UI showed `Failed: []` because the failure path
   used `check.get('failed_jobs', [])` but the activity returns the count
   under `failed`.

2. **`elastic-blast submit` from inside the pre-booted `elb-openapi` pod
   failed at `kubectl apply ... -o json` with `field is immutable` on
   leftover BLAST jobs from an earlier incomplete submit.** elastic-blast's
   own reuse-mode cleanup only runs when its `_db_already_loaded()` check
   passes, so a half-finished submit leaves stale `app=blast/submit/setup/
   finalizer` Jobs that the next submit cannot replace. A separate path
   triggered the same code with `outfmt = "7 std staxids ssciname"`; the
   embedded double-quotes broke the generated batch_*.yaml (`line 78: did
   not find expected key`).

User-visible symptom: AKS card showed "Warmup failed []" with no diagnostic;
job submit appeared to succeed in the orchestrator status but never produced
a BLAST Job in the cluster.

## Changes

### `api/activities/blast.py`

* New helper `_build_submit_args(config_b64, job_id)` builds the bash
  one-liner for both `_submit_via_k8s_exec` and `_start_submit_via_k8s_job`
  paths so future fixes apply once.
* Submit args now:
  * Retry `az login --service-principal` 5 × 5 s to ride out Workload
    Identity federated-token race on first scheduling.
  * Hard-code `PYTHONPATH=/opt/venv/lib/python3.11/site-packages` so the
    elastic-blast CLI (system python) can import `azure.mgmt.*` (venv).
  * `set -o pipefail`; abort with exit 2 if all 5 az login attempts fail.
* New helper `_cleanup_stale_blast_jobs(session, server)` deletes leftover
  Jobs labelled `app=blast|submit|setup|finalizer` from default ns before
  every submit. Idempotent, best-effort.
* `_submit_via_k8s_exec` and `_start_submit_via_k8s_job` now copy
  `ELB_*` env vars (in addition to `AZURE_*` / `AZCOPY_*`) from the running
  `elb-openapi` pod into the submit Job. Without this, the elastic-blast
  CLI falls back to discovery code that fails inside an isolated submit pod.
* `activity_k8s_warmup_db` init container retries azcopy 6 × 30 s before
  declaring failure. RBAC propagation now absorbed without surfacing as
  pod-level CrashLoopBackOff.
* `activity_k8s_check_warmup_db` only declares `status=failed` once a pod
  has accumulated ≥ 5 init container restarts (≈ 10–15 min). When it does
  fail it captures the last 60 lines of init container logs (sanitised) and
  surfaces them under `logs`, `failed_pod`, `init_failed`, `restart_max`.

### `api/orchestrators/warmup_db.py`

* New `RBAC_PROPAGATION_SECONDS = 60` timer between
  `assign_aks_roles_activity` and the warmup DaemonSet apply.
* Failure branch now reads `check.get('logs')` / `init_failed` /
  `restart_max` / `failed_pod` and renders a real error message instead of
  `Failed: []`.

### `api/services/blast_config.py`

* `outfmt` value is now rejected at the boundary (`ValueError`) when it
  contains shell/YAML-breaking characters (`"'`;&|$(){}\`). Failure
  surfaces in `generate_blast_config_activity` instead of 60 s later in
  the cluster.

## Validation

* `ruff check` + `py_compile` clean for changed files (lint count went 68 →
  62, no new warnings).
* Manual reproduction:
  * `kubectl exec -n default deploy/elb-openapi -- bash /tmp/test-submit.sh`
    on `elb-cluster-01` (16S_ribosomal_RNA, blastn) reached `[1/5] Writing
    configuration ...` → `Splitting queries` → `Upload workfiles` → reached
    `kubectl apply` step. Stale `field is immutable` no longer fires after
    `_cleanup_stale_blast_jobs` runs.
  * Warmup DaemonSet manually verified: after force-deleting failing pods,
    azcopy succeeded on retry. New retry loop should make manual delete
    unnecessary going forward.
* Deployed via `scripts/dev/deploy-api.sh` → `func-elb-prod-ga5754pr7jw3u`
  health probe returned 200.

## Out of scope (follow-ups)

* End-to-end blastn smoke test driven from the SPA (requires a finished
  cluster + small query set + 5 min runtime).
* `init-pv` job hangs on `configmap "elb-scripts" not found` if the
  ConfigMap is manually deleted; elastic-blast re-creates it via
  `_cleanup_stale_jobs` only on warm reuse. Worth a separate boundary
  check.
* `_submit_via_k8s_exec` retry around `kubectl apply` on transient
  500/conflict from kube-apiserver.
