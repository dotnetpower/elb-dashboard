# Cluster modal Workloads card — Pods / Deployments / Jobs tabs

## Motivation

The cluster detail modal's Kubernetes diagnostics only listed **Pods**. The
Azure portal Workloads pane shows Deployments, Jobs and more, and for an
ElasticBLAST cluster the Job → Pod relationship is the natural unit of work:
operators want to see finished/in-flight **Jobs** and the supporting
**Deployments** (frontend, CoreDNS, metrics-server) without leaving the
dashboard. A flat single list could not carry three resource shapes, so the
Pods section was promoted to a tabbed **Workloads** card.

## User-facing change

* The cluster modal's "Pods" section is now a collapsible **Workloads** card
  with three tabs: **Pods | Deployments | Jobs** (the subset that matters for
  ElasticBLAST; the portal's Replica sets / Stateful sets / Daemon sets / Cron
  jobs were intentionally omitted).
* Node-level diagnostics (**Node Resources**, **Nodes**) stay as sibling
  stacked sections — only the workload views are tabbed.
* Each tab has the same namespace filter and "N shown" indicator. Tab labels
  show a live item count once loaded.
  * **Pods** — unchanged behaviour (all phases, Node / Pod IP columns, Logs /
    Describe / Delete actions).
  * **Deployments** — read-only: NS / NAME / READY / UP-TO-DATE / AVAILABLE /
    AGE. READY turns amber when ready < desired.
  * **Jobs** — read-only: NS / NAME / COMPLETIONS / STATUS / DURATION / AGE.
    STATUS is colour-coded (Complete / Failed / Running / Pending).
* Tabs fetch lazily — opening the modal no longer fans out three Kubernetes
  API calls; a tab only loads when the card is expanded and the tab is active.
  "Refresh All" refetches node data and invalidates the live workload tab.

## API / IaC diff summary

* **New routes** (read-only, MSAL-gated, cluster-gated cache + `_graceful`
  degrade, same as `/aks/pods`):
  * `GET /api/monitor/aks/deployments` → `{ "deployments": K8sDeployment[] }`
  * `GET /api/monitor/aks/jobs` → `{ "jobs": K8sJob[] }`
* **New service helpers** in `api/services/k8s/monitoring.py` (re-exported via
  the `api/services/monitoring` facade):
  * `k8s_get_deployments(...)` — `apps/v1` list, `ready`/`up_to_date`/
    `available` parsing, optional namespace scoping.
  * `k8s_get_jobs(...)` — `batch/v1` list, `completions` and derived `status`
    (`Complete`/`Failed`/`Running`/`Pending`), `start_time`/`completion_time`.
* **No IaC change.** No new Storage/network surface, no SAS, no shell-out —
  direct Kubernetes API via the existing kubeconfig token path.
* **Frontend**: new `K8sDeployment` / `K8sJob` types + `k8sDeployments` /
  `k8sJobs` typed clients; new `K8sWorkloadsSection` (tab container) and
  `K8sDeploymentsPanel` / `K8sJobsPanel` / refactored `K8sPodsPanel`; shared
  `useNamespaceFilter` hook + `NamespaceFilter` component; `formatDuration`
  helper. Old `K8sPodsSection` deleted.

## Validation evidence

* `uv run pytest -q api/tests/test_k8s_get_deployments_jobs.py
  api/tests/test_k8s_get_pods.py` → 7 passed (new test covers endpoint URLs,
  namespace scoping, replica parsing, and all four derived Job statuses).
* `uv run pytest -q api/tests -k "aks or monitor or k8s"` → 402 passed (no
  regression from the facade / route additions).
* `uv run ruff check` on the touched backend files → All checks passed.
* `npx tsc --noEmit` → no errors in any ClusterDiagnostics / `monitoring.ts`
  file (pre-existing unrelated `SequenceDetail.tsx` errors untouched).
* `npx eslint src/components/ClusterDiagnostics src/api/monitoring.ts` → clean.
