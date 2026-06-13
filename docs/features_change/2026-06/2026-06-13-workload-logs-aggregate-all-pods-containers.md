---
title: Workload log views show every pod and container
description: AKS Pod/Deployment/Job log views now aggregate all matching pods and all containers instead of a single representative pod/container.
tags:
  - operate
  - blast
---

# Workload logs: show all pods and all containers

## Motivation

The cluster **Workloads** card (Pods / Deployments / Jobs tabs) only ever
displayed a **partial** view of workload logs:

- **Deployment / Job logs** selected a single *representative* pod
  (`_select_pod_for_logs`). A fan-out BLAST search Job produces one pod per
  query batch (plus retries when a Spot node is reclaimed), so the operator
  saw the logs of exactly one pod and none of the others.
- **Pod logs** fetched the Kubernetes pod-log endpoint **without a container
  name**. The endpoint serves one container at a time and 400s for a
  multi-container pod, so BLAST pods with an init container (DB download) plus
  a main container surfaced only the default container — or degraded to empty.

## User-facing change

- Deployment and Job log views now **aggregate every matching pod**, Running
  pods first, then newest, each block prefixed with `# logs from pod <name>`.
  The set is capped at 25 pods; when more exist a trailing
  `# … N more pod(s) not shown (showing newest 25 of M)` marker makes the
  truncation explicit (never a silent drop).
- Pod / Deployment / Job log views now show **every container** of each pod
  (init containers first), each prefixed with `--- container: <name> ---`.
  A single-container pod is rendered exactly as before (no header), so the
  common case is unchanged.

No API request/response shape changed: the routes still return
`{ "logs": "<text>" }` (plus the existing `degraded` / `degraded_reason`
graceful-degradation fields), so the SPA dialogs and typed clients need no
change.

## API / IaC diff summary

- `api/services/k8s/observability.py`: added `fetch_pod_all_container_logs`
  (+ `_list_pod_container_names`); `k8s_pod_logs` now delegates to it so a
  pod's logs cover all containers.
- `api/services/k8s/workload_ops.py`: replaced single-pod
  `_select_pod_for_logs` / `_fetch_pod_log_via_session` with
  `_select_pods_for_logs` (all matching pods, ordered) + `_aggregate_pod_logs`
  (per-pod, all-container blocks, `_MAX_LOG_PODS = 25` cap). `k8s_deployment_logs`
  and `k8s_job_logs` aggregate accordingly.
- No route, schema, Bicep, or frontend change.

## Validation evidence

- `uv run pytest -q api/tests/test_k8s_workload_ops.py` — 30 passed
  (new: `test_job_logs_aggregates_all_pods`, `test_job_logs_caps_pod_count`,
  `test_pod_logs_aggregate_all_containers`; updated
  `test_deployment_logs_selects_running_pod` to assert all pods appear).
- `uv run ruff check` on the three touched files — clean.
- `uv run pytest -q api/tests` — 3369 passed, 3 skipped.
