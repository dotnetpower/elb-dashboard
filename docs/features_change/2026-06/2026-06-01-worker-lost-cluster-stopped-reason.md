# Reconcile: refine opaque `worker_lost` into `cluster_stopped` / `cluster_not_found`

## Motivation

A BLAST submit that fails because its target [AKS](https://learn.microsoft.com/azure/aks/)
cluster is **stopped mid-flight** surfaced only the opaque `worker_lost`
error code on the Results page, with no actionable detail. Concretely:
job `50116a6b-…` (program `blastn`, db `16S_ribosomal_RNA`) was submitted
against `elb-cluster-01` while it was Running, the cluster was then stopped
(auto-stop or manual), `elastic-blast submit` / `kubectl` hung against the
now-unreachable API server, the worker went quiet, and the 60 s beat reconcile
demoted the stale row to a hardcoded `error_code="worker_lost"`. The researcher
saw "worker-lost" with no hint that the real cause was a powered-off cluster —
which in a multi-cluster deployment (the job's cluster can differ from the
dashboard anchor RG) is exactly the failure mode to call out.

The submit-time preflight gate already blocks a Stopped cluster
(`cluster_not_ready`, HTTP 409), so a job that reached the worker proves the
cluster was Running at submit and stopped *later* — precisely the case the
reconcile path must explain.

## User-facing change

- When the reconciler demotes a quiet/stale active row to failed, it now probes
  the **job's own** cluster (subscription / resource-group / cluster resolved
  from the job payload, not the workspace anchor) via the cached
  `get_cluster_health` ARM helper and refines the error code:
  - cluster reports `cluster_stopped` → `error_code="cluster_stopped"` with a
    human-readable history message: *"Target AKS cluster '<name>' is
    <power_state>. The in-flight job became unreachable before it finished.
    Start the cluster and resubmit."*
  - cluster reports `cluster_not_found` → `error_code="cluster_not_found"` with
    *"Target AKS cluster '<name>' no longer exists in '<rg>'. The job could not
    be completed."*
- When the cluster is healthy, the coordinates are incomplete, or the ARM probe
  fails, the code **falls back to the existing `worker_lost`** — no behaviour
  change for those cases (degrade-open, backward compatible).
- The Results page already humanizes `cluster_stopped` / `cluster_not_found`
  (`web/src/utils/monitorDegraded.ts` descriptors), so the researcher now sees
  an actionable status instead of bare "worker-lost". The rich sentence is
  recorded in the job history payload for deeper inspection.

## API / IaC diff summary

Backend only (`api/`) — no IaC, no frontend, no redeploy:

- `api/tasks/blast/reconcile_task.py`
  - new module-level helper
    `_worker_lost_reason(*, job_id, subscription_id, resource_group, cluster_name)
    -> tuple[str, dict[str, Any]]` (lazy-imports `get_cluster_health` +
    `get_credential` through the `api.services` wrappers — no direct
    `azure.mgmt.*` import, no Azure Run Command).
  - the worker-lost branch now computes `error_code, extra =
    _worker_lost_reason(...)` from the job's own resolved coordinates and passes
    them through `_update_state(... error_code=error_code, **extra)`. The
    `summary["worker_lost"]` counter and the `worker_lost` phase are unchanged.
  - `_worker_lost_reason` added to `__all__`; `reconcile_stale_jobs` docstring
    step 4 updated.

No response schema change: the API `error` field continues to surface
`error_code` (now `cluster_stopped` / `cluster_not_found` when applicable);
the human-readable `error` detail lands in the job history rows.

## Validation evidence

- `uv run pytest -q api/tests/test_blast_tasks.py -k reconcile` → **13 passed**
  (includes the two new tests below plus the backward-compat
  `test_reconcile_marks_old_quiet_row_worker_lost`).
- New tests in `api/tests/test_blast_tasks.py`:
  - `test_reconcile_worker_lost_refines_stopped_cluster` — stopped cluster →
    `error_code == "cluster_stopped"`, history carries `cluster_name`,
    `power_state`, and the human message.
  - `test_reconcile_worker_lost_keeps_plain_code_when_cluster_healthy` — healthy
    cluster → `error_code == "worker_lost"` (backward compat).
- `uv run pytest -q api/tests/test_blast_tasks.py` → **123 passed**.
- `uv run pytest -q api/tests` → **2379 passed, 3 skipped** (one unrelated
  real-subprocess test, `test_run_truncates_stdout_above_cap`, flaked under
  parallel load and passed in isolation).
- `uv run ruff check api/tasks/blast/reconcile_task.py api/tests/test_blast_tasks.py`
  → clean.
- Deployed evidence (moonchoi prod): `az aks list -g rg-elb-cluster` →
  `elb-cluster-01` **Stopped**, `elb-cluster-02` Running; App Insights showed
  repeated `ConnectionError` reaching the cluster around the failure window.
