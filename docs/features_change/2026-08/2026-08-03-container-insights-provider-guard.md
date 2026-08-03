---
title: Container Insights provider registration guard
description: Disable AKS Container Insights enablement unless Microsoft.OperationsManagement is registered.
tags: [operate, infra, ui]
---

# Container Insights provider registration guard

## Motivation

Enabling [Container Insights](https://learn.microsoft.com/azure/azure-monitor/containers/kubernetes-monitoring-enable)
on an AKS cluster creates an Operations Management solution linked to the Log
Analytics workspace. The subscription must first register the
[`Microsoft.OperationsManagement` resource provider](https://learn.microsoft.com/azure/azure-resource-manager/management/resource-providers-and-types).

The Settings action previously enqueued an AKS `omsagent` addon update without
checking that prerequisite. In the investigated subscription,
`Microsoft.OperationsManagement` was `NotRegistered` while
`Microsoft.OperationalInsights` was `Registered`. Azure rejected the addon
update after it had entered the AKS create-or-update operation.

## User-facing change

- **Settings → AKS Observability** now shows the provider registration state.
- **Enable Container Insights** remains disabled unless the provider is
  explicitly `Registered`.
- A direct enable API request receives HTTP 409 with the stable code
  `container_insights_provider_not_registered` (or
  `container_insights_provider_status_unavailable` when the read cannot be
  completed).
- Disable remains available regardless of provider state.
- An already queued or automatic enable task re-checks provider state before
  any workspace-RG RBAC write or AKS mutation and returns an `enabled=false`
  skipped result when the prerequisite is unavailable.

The dashboard does not register or unregister the provider automatically. That
subscription-level operation remains an explicit Azure administrator action.

## API and implementation summary

- The AKS observability service adds a read-only provider-status projection.
- The status API adds backward-compatible provider and enable-availability
  fields.
- The enable route and Celery task enforce the same fail-closed decision,
  closing the route-to-task race.
- The SPA treats only `enable_available=true` as actionable and renders the
  provider state as a Settings badge.

No RBAC assignment, provider registration, network rule, or AKS resource was
changed while implementing or validating this guard.

## Validation

- Read-only live provider projection: `Microsoft.OperationsManagement` →
  `NotRegistered`, `enable_available=false`,
  `enable_unavailable_reason=provider_not_registered`.
- `uv run pytest -q api/tests/test_aks_observability_service.py api/tests/test_settings_aks_observability.py api/tests/test_aks_observability_task.py api/tests/test_azure_provision_aks.py`
- `uv run ruff check api`
- `npm --prefix web run test -- --run`
- `npm --prefix web run build`
- `uv run pytest -q api/tests`
- `DISABLE_MKDOCS_2_WARNING=true uv run mkdocs build --strict`
