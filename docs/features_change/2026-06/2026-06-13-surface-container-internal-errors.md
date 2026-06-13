---
title: Container-internal errors surfaced in workload logs, describe, and status
description: Pod/Job/Deployment log views now show previous-instance crash output and waiting reasons; describe shows init containers; the Pods list shows the real container status (CrashLoopBackOff, Init:Error) instead of the misleading phase.
tags:
  - operate
  - blast
---

# Surface container-internal errors across the Workloads card

## Motivation

A follow-up to the all-pods/all-containers log change: operators reported that
when a container actually **failed**, the Workloads card still showed almost
nothing, making diagnosis hard. An exhaustive survey of the log/status paths
found four places where container-internal errors were swallowed:

1. **Crashed/restarted containers** — the log endpoint was only read for the
   *current* (restarted, often empty) instance. A crash's real output lives in
   the **previous** instance (`kubectl logs --previous`), which was never
   fetched, so the actual failure was invisible.
2. **Waiting containers** — when a container is `CrashLoopBackOff` /
   `ImagePullBackOff` / `PodInitializing`, the log GET 400s. The view showed a
   bare `(log unavailable: HTTPError)` instead of the kubelet's waiting reason
   and message.
3. **Init containers in Describe** — `_format_pod_describe` only rendered
   `spec.containers`, so a failed BLAST DB-download **init** container's
   terminated reason / exitCode / message never appeared — the pod looked
   healthy with no clue why it was stuck.
4. **Pods list STATUS** — the column showed `status.phase`, which reads
   `Running` for a CrashLoopBackOff pod and `Pending` for an ImagePullBackOff
   pod. The real container error reason was completely hidden from the list.
5. **BLAST live/persisted log stream skipped init containers** — a follow-up
   re-survey found `discover_k8s_log_targets` (the per-job SSE timeline +
   completed-job persistence path) iterated only `spec.containers`. The
   ElasticBLAST batch search pod runs `import-query-batches` as an **init**
   container; when it fails the main `blast` container never starts (empty
   log), so a failed search showed no error in its live timeline at all.

## User-facing change

- **Logs (Pods / Deployments / Jobs)**: each container block now carries a
  state-annotated header (`--- container: <name> [Terminated exit 1 (Error)] ---`).
  When a container has restarted, the **previous instance** log is appended
  (`--- container: <name> (previous instance, restarts=N) ---`). When the log
  GET fails because the container is waiting, the kubelet reason + message is
  shown instead of a bare error. A healthy single-container pod is unchanged
  (raw body, no header) so the calm common case stays the same.
- **Describe (Pods)**: a new `Init Containers:` section renders init containers
  with the same Image / Ready / Restart Count / State (Terminated exit/reason +
  Message) detail as regular containers.
- **Pods list STATUS**: now mirrors `kubectl get pods` — surfaces the failing
  container's reason (`CrashLoopBackOff`, `Error`, `ImagePullBackOff`,
  `Init:Error`, `ExitCode:N`, `Completed`, …) instead of the misleading phase.
  The SPA already colours `crash`/`error` substrings red, so failing pods now
  stand out without any frontend change.

No API request/response **shape** changed: log/describe routes still return
`{ "logs"|"describe": "<text>" }`; the pods list still returns the same
`status` string field (now more accurate). The SPA needs no change.

## API / IaC diff summary

- `api/services/k8s/observability.py`:
  - `fetch_pod_all_container_logs` rewritten to read pod status, annotate each
    container block with its state, surface waiting reason/message on log-GET
    failure, and fetch `previous=true` logs for restarted containers
    (`restartCount > 0` only). New helpers `_index_container_statuses`,
    `_container_state_summary`, `_render_container_block`.
  - `_format_pod_describe` now renders an `Init Containers:` section; the
    per-container block was extracted to `_append_container_describe` and
    reused for init + regular containers.
  - New exported `compute_pod_display_status` (kubectl-style STATUS printer,
    including `Init:` prefixes and `Terminating`).
- `api/services/k8s/monitoring.py`: `k8s_get_pods` now sets `status` from
  `compute_pod_display_status(item)` instead of `status.phase`.
- `api/services/job_logs/k8s.py`: `discover_k8s_log_targets` now iterates
  `spec.initContainers` + `spec.containers` (deduped) so a failed BLAST init
  container streams/persists; `_pod_env_has_value` scans init containers too.
- No route, schema, Bicep, or frontend change.

## Validation evidence

- New `api/tests/test_pod_container_logs.py` (4 tests: previous-instance fetch,
  waiting-reason surfacing, clean single-container raw-body compat, terminated
  single-container state).
- `api/tests/test_k8s_pod_describe.py`: `test_format_pod_describe_renders_failed_init_container`.
- `api/tests/test_k8s_get_pods.py`: `test_k8s_get_pods_surfaces_crashloop_status`,
  `test_k8s_get_pods_surfaces_init_failure_status`.
- `api/tests/test_job_log_k8s.py`:
  `test_discover_k8s_log_targets_includes_init_containers`.
- `uv run pytest -q api/tests/test_pod_container_logs.py
  api/tests/test_k8s_pod_describe.py api/tests/test_k8s_get_pods.py
  api/tests/test_k8s_workload_ops.py` — 44 passed.
- `uv run ruff check` on all touched files — clean.
- Full suite: the two unrelated failures
  (`test_control_plane_env::test_bicep_references_every_guard_key`,
  `test_tasks_facade_contract`) stem from in-progress `SERVICEBUS_ENABLED` /
  `enable_aks_container_insights` work in other dirty files, not this change.
