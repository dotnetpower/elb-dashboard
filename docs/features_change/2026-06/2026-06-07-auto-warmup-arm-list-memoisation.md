---
title: Auto-warmup reconcile memoises the ARM cluster list per tick
description: The auto-warmup beat reconcile now lists AKS clusters once per (subscription, resource group) per tick instead of once per preference, cutting redundant ARM managedClusters round trips.
tags:
  - operate
  - architecture
---

# Auto-warmup reconcile memoises the ARM cluster list per tick

## Motivation

An App Insights dependency-volume hunt on the live deployment showed
`GET .../Microsoft.ContainerService/managedClusters` (ARM `managedClusters.list`)
being called ~3,300 times in 4 hours across the api + worker roles. ARM has a
per-subscription read rate limit, so trimming avoidable list calls reduces the
risk of throttling as the number of enrolled clusters / preferences grows.

## Root cause

`reconcile_auto_warmup_preferences` loops over every persisted Auto-warm
preference and called `monitoring.list_aks_clusters(sub, rg)` **once per
preference**. When several preferences live in the same resource group (the
common multi-database case — one preference per DB list, or several clusters in
one RG), each preference triggered its own ARM `managedClusters.list` even though
they would all read the same instantaneous snapshot.

## Fix

A per-call (per-tick) memo `dict[(sub, rg) -> clusters]` (`_clusters_for`) lists
the clusters once per `(subscription, resource group)` and reuses the result for
the remaining preferences in that tick. The cache is a local variable scoped to
the single reconcile call, so it never outlives the tick and introduces no
staleness — every preference in one tick already expects the same snapshot.

No behaviour change: the readiness gate and warmup decisions are identical; only
the number of ARM round trips drops (from N-preferences to N-distinct-(sub,rg)).

## Validation

- New regression test `test_reconcile_memoises_cluster_list_per_subscription_rg`
  asserts two same-RG preferences trigger exactly one `list_aks_clusters` call.
- `uv run pytest -q api/tests/test_auto_warmup.py` — 32 passed.
- `uv run ruff check` clean.

## Notes

This is a cost / throttle-risk reduction, not a correctness fix. Per-preference
*Kubernetes* reads (`k8s_ready_warmup_node_names`, `k8s_warmup_status`) are left
per-cluster because they are cluster-scoped and not the ARM hotspot.
