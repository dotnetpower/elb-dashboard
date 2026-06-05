---
title: Two-colour node memory bar — working set vs. warm file cache
description: Node Resources bars now split reclaimable BLAST page cache from working-set memory so a warmed-but-idle node no longer looks "1% used".
tags:
  - ui
  - blast
---

# Two-colour node memory bar (working set vs. file cache)

## Motivation
The Cluster Diagnostics → **Node Resources** memory bar read the
`metrics.k8s.io` node `usage.memory`, which is the **working set** and
deliberately *excludes* reclaimable file (page) cache. A warmed BLAST DB lives
entirely in page cache, so a blastpool node holding ~28 GiB of `core_nt` in RAM
still rendered as `1.5 / 126 GiB (1%)`. Users reasonably read that as "plenty of
free memory" and could not reconcile it with the warmup planner blocking large
DBs (`safe budget 64 GiB exceeded`) — the bar and the planner appeared to
contradict each other, when in fact they measure different things.

## User-facing change
Each node's memory bar is now a stacked two-colour bar when the kubelet exposes
cache stats:

- **Purple** segment — working set (in-use memory), unchanged.
- **Teal** segment — reclaimable file cache (the warm BLAST DB), stacked beside
  it. The row label gains a `+<N> cache` suffix and a legend explains the two
  colours.

When the kubelet `/stats/summary` proxy is unreachable (e.g. the cluster's
kubeconfig identity lacks the `nodes/proxy` verb, or a node times out), the bar
renders working-set-only exactly as before — the cache overlay is purely
additive and never blocks the panel.

## API / IaC diff summary
- `api/services/k8s/node_cache.py` (new) — `fetch_node_cache_ki()` samples the
  kubelet Summary API per node in parallel (bounded at 8 workers, 5 s timeout)
  and derives cache as `usageBytes - workingSetBytes`. Best-effort: never
  raises; a denied/slow node is silently omitted.
- `api/services/k8s/metrics.py` — `k8s_top_nodes()` enriches each node with
  optional `cache_ki` / `cache_pct` via a guarded `_enrich_with_page_cache()`
  helper. Same denominator (node capacity) as `memory_pct`, so the stacked
  segments are comparable. No change to existing fields.
- `web/src/api/monitoring.ts` — `K8sNodeMetrics` gains optional `cache_ki` /
  `cache_pct` (additive, backward compatible).
- `web/src/components/ClusterDiagnostics/NodeResourcesSection.tsx` — memory
  `UsageBar` accepts an optional stacked `overlay` segment; legend added when
  any node reports cache. CPU bar untouched.
- `web/src/mocks/docsPreview.ts` — top-nodes mock node carries cache fields so
  the docs preview shows the two-colour bar.
- No backend RBAC (Azure roleAssignments) change. The data source is the
  cluster-internal kubelet proxy reached with the existing kubeconfig
  credential; if that credential lacks `nodes/proxy`, the feature degrades
  gracefully rather than erroring.

## Validation evidence
- `uv run pytest -q api/tests/test_k8s_node_cache.py api/tests/test_k8s_top_nodes_cache.py`
  — 13 new tests (happy path, negative-cache clamp, partial failure, malformed
  payload drop, proxy-denied degradation) pass.
- `uv run pytest -q api/tests` — 2913 passed, 3 skipped (pre-existing).
- `uv run ruff check api` — clean.
- `cd web && npm run build` — succeeds; `npx eslint` on touched files clean;
  `npx vitest run src/components/ClusterDiagnostics src/components/warmupSection`
  — 6 passed.
