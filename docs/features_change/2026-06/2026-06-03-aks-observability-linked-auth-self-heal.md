---
title: Self-heal LinkedAuthorizationFailed when enabling AKS Container Insights
description: Self-grant Contributor to the dashboard managed identity on the Log Analytics workspace's resource group so enabling Container Insights succeeds from the browser instead of failing with LinkedAuthorizationFailed.
tags:
  - operate
---

# AKS Observability: self-heal LinkedAuthorizationFailed when enabling Container Insights

## Motivation

Enabling Container Insights from the dashboard Settings panel patches the AKS
cluster's `omsagent` addon. That patch additionally creates a
`ContainerInsights(<workspace>)` OMS solution **in the Log Analytics
workspace's resource group**, which requires
`Microsoft.OperationsManagement/solutions/write` on that RG — a *linked scope*
relative to the cluster.

In the moonchoi subscription the cluster is wired to Azure's auto-created
default workspace `defaultworkspace-…-se` in `defaultresourcegroup-se`, an RG
that is outside this deployment's IaC and on which the shared managed identity
holds no role. ARM therefore rejected the addon patch with
`(LinkedAuthorizationFailed)`, leaving the feature permanently broken from the
browser. An earlier same-day fix
([lowercase workspace id](2026-06-03-aks-observability-lowercase-workspace-id.md))
let the request reach ARM; this fix makes it actually succeed.

## User-facing change

Clicking **Enable Container Insights** now succeeds end-to-end without any
manual Azure Portal step (browser-only charter). Before patching the addon the
enable task self-grants **Contributor** to the dashboard managed identity on the
workspace's resource group, then retries the (idempotent) addon patch within a
bounded window while the new role assignment propagates.

If the self-grant cannot be performed (e.g. an older deployment whose MI lacks
`roleAssignments/write`), the task no longer fails with an opaque ARM error.
Instead it raises an actionable message carrying the exact recovery command:

```
az role assignment create --assignee <mi-object-id> --role Contributor \
  --scope /subscriptions/<sub>/resourceGroups/<workspace-rg>
```

## API / IaC diff summary

No HTTP contract or IaC change. Backend-only:

- `api/tasks/azure/rbac.py` — new best-effort helper
  `ensure_dashboard_mi_resource_group_contributor(...)` that self-grants
  Contributor to the MI on an arbitrary RG using a stable `uuid5` assignment id
  (idempotent; `RoleAssignmentExists` treated as success). Contributor is on
  the existing `Elb Workload RG Creator` ABAC whitelist
  (`infra/modules/workloadRgCreatorRole.bicep`), so no infra change is needed.
- `api/tasks/azure/__init__.py` — re-export the helper as
  `_ensure_dashboard_mi_resource_group_contributor` (facade monkeypatch pattern)
  and add it to `__all__`.
- `api/tasks/azure/aks_observability.py` — `enable_aks_container_insights` now
  parses the workspace RG, best-effort self-grants Contributor there, and wraps
  the addon enable in a bounded retry on `LinkedAuthorizationFailed`
  (`_LINKED_AUTH_RETRY_SECONDS = 150s`, exponential 10→30s backoff). Self-grant
  failures never abort the enable; non-linked-auth errors are not retried. The
  returned service `state` dict is unchanged (additive-safe).

## Validation evidence

- `uv run pytest -q api/tests/test_aks_observability_task.py` → **6 passed**
  (self-grant targets the parsed workspace RG; LinkedAuthorizationFailed retried
  then success; retry exhaustion raises the actionable recovery command;
  non-linked-auth error not retried; self-grant exception does not abort;
  unparseable workspace id skips the grant but still enables).
- `uv run pytest -q api/tests/test_azure_tasks.py api/tests/test_settings_aks_observability.py`
  → **40 passed** (no regression in existing RBAC / route tests).
- `uv run ruff check api/tasks/azure api/tests/test_aks_observability_task.py`
  → **All checks passed**.
- Full sweep `uv run pytest -q api/tests` → **2553 passed, 3 skipped, 1 failed**;
  the single failure is `test_terminal_exec.py::test_run_truncates_stdout_above_cap`,
  a subprocess timeout test (exit 124 under parallel-suite machine load) in an
  unrelated module this change does not touch.

## Operational note

The self-heal grants **Contributor** to the shared managed identity on the Log
Analytics workspace's resource group (commonly the non-IaC
`defaultresourcegroup-<loc>`). The grant is additive, idempotent, and
reversible, and mirrors the existing
`ensure_dashboard_mi_cluster_rg_roles` precedent.
