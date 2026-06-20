---
title: Cluster Workloads Pods/Jobs — newest-first infinite scroll
description: The cluster Workloads card's Pods and Jobs tabs now sort newest-first and render 20 rows at a time with infinite scroll instead of painting the entire roster.
tags:
  - ui
  - user-guide
---

# Cluster Workloads Pods/Jobs — newest-first infinite scroll

## Motivation

On a long-lived ElasticBLAST cluster the Workloads card's **Pods** and **Jobs**
tabs rendered every row at once. A real cluster accumulated 500+ pods and
1000+ Jobs, so the tables painted hundreds of rows in creation order (oldest
first), burying the rows an operator actually cares about — the most recent
ones — at the bottom of a very long scroll.

## User-facing change

* The **Pods** and **Jobs** tabs of the cluster Workloads card now:
  * **Sort newest-first** by Kubernetes `age` (creationTimestamp) — the most
    recently created pod/job is at the top.
  * **Render 20 rows at a time** inside a fixed-height (≈460px) scroll
    viewport, growing the window by 20 as the operator scrolls (infinite
    scroll via `IntersectionObserver`).
  * Show a compact footer: `Showing 20 / 506 · scroll for more`, or
    `506 total` once everything is rendered.
* The window resets to the first 20 rows when the namespace filter changes or a
  refetch returns a different snapshot, so the operator is never parked deep in
  a stale tail.
* **Deployments** tab is unchanged (its roster is small).
* No backend, API, or payload change — this is purely client-side ordering and
  windowing. The namespace filter's "N shown" count is unchanged (it still
  reflects the total rows in the selected namespace).

## Implementation summary

* New hook [web/src/components/ClusterDiagnostics/useAgeSortedInfinite.ts](../../../web/src/components/ClusterDiagnostics/useAgeSortedInfinite.ts)
  — sorts by `age` descending and exposes a growing `visible` prefix plus the
  scroll/sentinel refs.
* New presentation wrapper [web/src/components/ClusterDiagnostics/WorkloadScroll.tsx](../../../web/src/components/ClusterDiagnostics/WorkloadScroll.tsx)
  — fixed-height scroll container, sentinel, and `N / total` footer.
* [web/src/components/ClusterDiagnostics/K8sPodsPanel.tsx](../../../web/src/components/ClusterDiagnostics/K8sPodsPanel.tsx)
  and [web/src/components/ClusterDiagnostics/K8sJobsPanel.tsx](../../../web/src/components/ClusterDiagnostics/K8sJobsPanel.tsx)
  now render `visible` instead of the full filtered list.

## Validation evidence

* `cd web && npm run build` → `tsc -b && vite build` ✓ built (no type errors).
* `npx eslint` on the four touched/added files → clean (exit 0).
* No ClusterDiagnostics unit tests exist; the panels' only consumer
  (`K8sWorkloadsSection`) passes unchanged props.
