---
title: Incident response runbook
description: Operator runbook for the common elb-dashboard failure scenarios — cluster/node down, job failures, cost spikes, capacity saturation, terminal sidecar loss, and storage network blocks — each mapped to the dashboard signal, the automatic response already in place, and the manual action.
tags:
  - operate
---

# Incident response runbook

This runbook covers the failure scenarios an operator is most likely to hit and,
for each, **(1)** the dashboard signal, **(2)** the automatic response already in
place, and **(3)** the manual action. The control plane is browser-only — prefer
the dashboard action; CLI is given only as a fallback.

> Every automatic threshold below is environment-tunable. The defaults are the
> shipped values; see the [Feature gate registry](feature-gates.md) for the gated
> ones.

---

## 1. Cluster or node down

**Signal.** The Cluster card shows the [AKS](https://learn.microsoft.com/azure/aks/)
cluster as `Stopped` / `Failed` / not found, or running jobs freeze with a
"status frozen — cluster stopped" badge.

**Automatic response.**

* The **stale-job reconciler** (`reconcile_stale_jobs`,
  [Celery](https://docs.celeryq.dev/) beat every 90 s) re-syncs rows the worker
  abandoned: a job quiet for more than its stale threshold (600 s) and unknown to
  K8s/OpenAPI is moved to `worker_lost`, then refined to `cluster_stopped` /
  `cluster_not_found` once the cluster state is probed. Before marking a job lost
  it checks for the result `SUCCESS.txt` marker, so a job that actually finished
  while the cluster was stopping is recovered instead of failed.
* A per-cluster **circuit breaker** (`cluster_breaker`) trips after 2 consecutive
  K8s connect failures and short-circuits further calls for a 120 s cooldown, so
  a stopped cluster does not flood the logs or stall every monitor tick.

**Manual action.**

1. Confirm cluster power state on the Cluster card.
2. If intentionally stopped, **Start** it from the dashboard (or `az aks start`).
   The auto-stop machinery records the start so cost/uptime estimates resume.
3. If provisioning failed, check the AKS provisioning state and re-run the
   provisioning wizard; recently-failed provisions surface on the cluster card.

---

## 2. BLAST job failed

**Signal.** A job row shows `Failed` with an `error_code`. The job API now
attaches a `failure_classification` (category + `auto_retryable`) to every failed
job so the cause family is explicit.

**Automatic response.**

* **Failure classification** (`failure_classification.py`) is the single source of
  truth. Only *transient submit-phase infrastructure* failures (terminal sidecar
  / Azure auth / node-warmup) are `auto_retryable`. K8s runtime failures
  (`blast_search_failed`), cluster-state failures, and configuration failures are
  not — retrying them is wasteful or impossible.
* **Auto-retry sweep** (`blast-auto-retry-failed-jobs`, default-OFF behind
  `BLAST_AUTO_RETRY_ENABLED`): when enabled, transient failures are resubmitted
  with exponential backoff (max 2 attempts, then **quarantined**), bounded per
  pass. See the [Feature gate registry](feature-gates.md) for tunables.

**Manual action by category.**

| Category | What it means | Action |
| --- | --- | --- |
| `transient_infra` | Submit never reached the cluster | Re-run (Duplicate) or enable the auto-retry gate |
| `runtime` (`blast_search_failed`) | Search failed on the cluster | Inspect logs, fix the query/DB, then re-run |
| `cluster_state` | Cluster stopped/missing | Resolve scenario 1, then re-run |
| `permanent` | Config/contract error | Fix the submit parameters; retrying changes nothing |

Use the job's **Duplicate / Re-run** action to resubmit with the same parameters
(the query must be re-entered). Live pod logs stream on the job detail page.

---

## 3. Cost spike / over budget

**Signal.** The **Cost estimate** card shows a rising projected-monthly figure or
an over-budget warning (when a monthly budget threshold is set).

**Automatic response.**

* **Idle auto-stop** (`auto_stop`) stops an idle cluster after its idle window
  (default 60 min; selectable 15/30/60/120/240) so a forgotten running cluster
  stops billing compute. This is the primary cost guardrail.

**Manual action.**

1. Read the Cost card: hourly rate × node count drives the estimate. A high rate
   usually means a large node pool left running.
2. **Stop** the cluster from the dashboard if no jobs are active, or lower the
   auto-stop idle window.
3. Set or tighten the **monthly budget** on the Cost card to get an earlier
   warning next time.
4. The dashboard estimate is approximate (workload pool only, assumes 24/7);
   confirm real spend in
   [Azure Cost Management](https://learn.microsoft.com/azure/cost-management-billing/).

---

## 4. Capacity saturation (too many concurrent jobs)

**Signal.** New submits sit in a queued/waiting phase; the cluster card shows
pending pods or node pressure.

**Automatic response.**

* The **capacity admission gate** (`capacity_gate`, default-OFF behind
  `BLAST_GATE_ENABLED`) caps concurrent submits per cluster (default 1 slot) and
  re-enqueues a contended submit with backoff instead of overcommitting. The
  `/api/blast/capacity` preview reports the would-be decision even when the gate
  is off.

**Manual action.**

1. Check how many jobs are `running` on the Cluster/Jobs cards.
2. Scale the workload node pool up (cluster scale action) if the workload
   warrants it, or enable the capacity gate to serialise submits.
3. Wait for in-flight jobs to drain; queued submits admit automatically.

---

## 5. Terminal sidecar unavailable

**Signal.** Submits fail with `terminal_exec_unavailable` /
`terminal_sidecar_unavailable` / `exec_token_missing`; the browser terminal does
not connect.

**Automatic response.** These are classified `transient_infra`, so the auto-retry
gate (when enabled) will resubmit once the sidecar recovers.

**Manual action.**

1. Check the Sidecars card for the `terminal` sidecar CPU/health.
2. If the sidecar is unhealthy, the [Azure Container Apps](https://learn.microsoft.com/azure/container-apps/)
   revision may need a restart (maintainer action — the sidecars share one
   revision).
3. Re-run affected submits after the sidecar is healthy.

---

## 6. Storage data plane blocked

**Signal.** The BLAST Databases / Queries / Results screens render a
`network_blocked` degraded state.

**Cause (by design).** Workload [Azure Storage](https://learn.microsoft.com/azure/storage/)
is `publicNetworkAccess: Disabled` in production; only the Container App reaches
it over private endpoints. A developer iterating from a laptop cannot reach the
data plane — this is expected, not an incident.

**Manual action.** For local debugging only, open the surface to your IP with the
sanctioned helper (`scripts/dev/local-run.sh storage-on`) and close it again
(`storage-off`) when done. In a deployed environment, `publicNetworkAccess`
remaining `Enabled` after debugging **is** an incident — close it. Never add a
production code path that flips it.

---

## 7. Where to look

| Surface | What it tells you |
| --- | --- |
| Cluster card | Power state, node SKU/count, provisioning, recent failed provisions |
| Jobs cards | Per-job status/phase, failed-rate, `failure_classification` |
| Cost card | Approximate hourly/monthly compute cost, budget warning |
| Sidecars card | `api` / `worker` / `terminal` / `redis` health and CPU/MEM |
| Notification bell | Recent terminal jobs (completed/failed/cancelled) at a glance |
| Job detail | Live pod logs, execution-step timeline, error detail |

For deeper telemetry the control plane emits structured lifecycle events to
App Insights (`blast`, `warmup`, `cluster_provision`, `prepare_db`,
`blast_auto_retry`); query `customEvents` for terminal-status trends.

---

## Escalation

Escalate to the maintainer when: a Container App revision restart is needed; a
cluster will not start after `az aks start`; `publicNetworkAccess` is stuck
`Enabled` in a deployed environment; or repeated `quarantined` auto-retry jobs
indicate a systemic submit-phase fault rather than a transient blip.
