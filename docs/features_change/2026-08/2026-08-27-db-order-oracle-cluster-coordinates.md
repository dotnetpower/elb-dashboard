---
title: DB order oracle cluster coordinates
description: Target DB order-oracle jobs at the discovered AKS workload cluster when Storage and AKS use different resource groups.
tags: [blast, ui]
---

# DB order oracle cluster coordinates

## Motivation

The Storage card discovered the active [AKS](https://learn.microsoft.com/azure/aks/what-is-aks) workload cluster, but the Build Oracle action still submitted the legacy `elb-cluster` name under the Storage resource group. Deployments that keep Storage and AKS in separate resource groups therefore received `cluster_not_found` even while the real cluster was Running.

## User-facing change

Build Oracle now targets the same subscription-wide workload cluster shown by the Storage card. A running cluster in a dedicated resource group is no longer reported as missing, and the button's readiness gate evaluates that exact cluster.

## API and infrastructure summary

- `POST /api/blast/databases/{db_name}/oracle` accepts optional `aks_resource_group` while retaining `resource_group` as the Storage scope.
- ARM health, warmup status, Ready-node discovery, and Kubernetes Job creation all use the AKS resource group.
- Omitting `aks_resource_group` preserves the previous same-resource-group behavior for existing clients.
- No infrastructure, RBAC, network, authentication, or Storage access setting changed.

## Validation

- `uv run pytest -q api/tests/test_blast_oracle_aks_route.py` - `4 passed`.
- `uv run ruff check api` and `uv run pytest -q api/tests` - `5314 passed, 4 skipped`.
- `npm --prefix web test`, `npm --prefix web run lint`, and `npm --prefix web run build` - `978 passed`; lint and build passed.
- VS Code browser request capture confirmed separate `resource_group`, `aks_resource_group`, and discovered `cluster_name` values.
- On the current deployment, the legacy request targeted `rg-elb-dashboard / elb-cluster`; correcting that one request to the discovered `rg-elb-cluster / elb-cluster-01` target produced run `20260827074309-dbacd590`, whose 10 Kubernetes Jobs succeeded and whose Storage status converged to `ready` with `10/10` parts.