# Workloads: Logs / Describe / Delete for Deployments and Jobs

## Motivation

The cluster Workloads card already exposed per-row **Logs / Describe / Delete**
actions for Pods, but the Deployments and Jobs tabs were read-only. Operators
had to open the browser terminal and run `kubectl` by hand to inspect or remove
a Deployment / Job — a charter violation (every action should be drivable from
the UI). This brings the two remaining tabs to parity with Pods.

## User-facing change

In the AKS cluster **Workloads** card:

- **Deployments** tab rows now have **Logs**, **Describe**, and **Delete**
  buttons.
- **Jobs** tab rows now have the same three buttons.
- **Logs** for a Deployment / Job tail the last 200 lines of a representative
  pod (prefers a `Running` pod, falls back to the newest one) and the output is
  prefixed with `# logs from pod <name>` because the workload can own many pods.
- **Describe** renders a `kubectl describe`-style block (replica/condition
  summary for Deployments; parallelism / completions / active-succeeded-failed
  for Jobs) plus recent events.
- **Delete** opens a confirm dialog and removes the Deployment (Foreground
  propagation, so its pods go too) or Job (Background propagation). The button
  is hidden for system-managed namespaces, and the backend route independently
  refuses them (403) — frontend-only gating would be an OWASP A01 issue.

## API / IaC diff summary

New `api` routes (all under `/api/monitor/aks`, bearer-protected via
`require_caller`):

| Method | Path | Returns |
| --- | --- | --- |
| GET | `/aks/deployment-logs` | `{ logs }` |
| GET | `/aks/deployment-describe` | `{ describe }` |
| DELETE | `/aks/deployment` | `{ status, kind, namespace, name, status_code, detail? }` |
| GET | `/aks/job-logs` | `{ logs }` |
| GET | `/aks/job-describe` | `{ describe }` |
| DELETE | `/aks/job` | `{ status, kind, namespace, name, status_code, detail? }` |

New service module `api/services/k8s/workload_ops.py` provides the six
`k8s_deployment_*` / `k8s_job_*` helpers, reusing `observability.py`'s name
guards (`_SAFE_K8S_NAME_RE`), `SYSTEM_NAMESPACES` delete gate, and event
formatting. Re-exported through `api/services/k8s/monitoring.py` and the
`api/services/monitoring` package so routes import them via `monitoring_svc`.
All Kubernetes calls go through the existing `_get_k8s_session` direct-API
helper — no Azure Run Command.

Frontend:

- `web/src/api/monitoring.ts` — added `k8sDeploymentLogs/Describe/Delete` and
  `k8sJobLogs/Describe/Delete` typed clients.
- New shared hook `useWorkloadActions.tsx` owns the Logs/Describe/Delete dialog
  lifecycle, the `SYSTEM_NAMESPACES` button gate, and renders the action
  buttons + dialog stack for any workload kind. `K8sPodsPanel` was refactored
  onto it (removing its duplicated lifecycle), and `K8sDeploymentsPanel` /
  `K8sJobsPanel` adopt it.
- `PodLogsDialog` / `PodDescribeDialog` generalized: `target.pod` → `target.name`
  plus a `kind?: string` (default `"Pod"`) so titles read `<kind> Logs` /
  `<kind> Describe`.
- `K8sWorkloadsSection` now threads `subscriptionId` / `resourceGroup` /
  `clusterName` into the Deployments and Jobs panels and wires `refetch` for
  post-delete refresh.

No IaC change.

## Validation evidence

- `uv run pytest -q api/tests/test_k8s_workload_ops.py` — 27 passed (new suite:
  system-namespace refusal, invalid-name `ValueError`, status-code mapping
  deleted/not_found/error, Foreground vs Background propagation, representative
  pod selection for logs, Deployment/Job describe formatting).
- `uv run pytest -q api/tests` — 2553 passed, 3 skipped, 1 pre-existing flaky
  failure (`test_terminal_exec.py::test_run_truncates_stdout_above_cap`, passes
  in isolation, unrelated to this change).
- `uv run ruff check api` — clean.
- `cd web && npm run build` — built in 15.9 s, no type errors.
