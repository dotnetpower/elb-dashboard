# 2026-06-08 — Cluster workload-node scaling with automatic re-warm

## Motivation

Operators could provision a cluster at a fixed node count but had no way to
resize the workload pool afterwards (e.g. shrink 10 nodes to 5 to save cost, or
grow it for a larger batch) without going to the Azure portal / CLI. And because
the node-local BLAST DB cache is per-node, a resize would otherwise leave
freshly-added nodes cold.

## User-facing change

- The expanded cluster card now shows a **Workload nodes** panel (next to
  Auto-stop) with a slider + number input (always in sync) and an **Apply**
  button. Available only while the cluster is Running; gated behind the caller's
  `can_write` RBAC.
- Applying a new count resizes the workload (blastpool) pool. When Auto warm
  databases are configured, a forced warm-up reconcile runs automatically after
  the resize so newly-added nodes get their node-local BLAST DB cache and the
  warm-up status tracks the new pool size. The panel states whether re-warm will
  run.
- Picking the current count (no change) disables Apply in the UI. If a no-op
  scale still reaches the backend (an autoretry racing ARM's convergence after a
  successful pool PUT), the re-warm is still ensured so freshly-added nodes never
  silently stay cold — the re-warm is idempotent (already-cached nodes are
  skipped).

## API / IaC diff summary

- **New route** `POST /api/aks/scale` ([api/routes/aks/lifecycle.py](../../api/routes/aks/lifecycle.py)):
  validates `node_count` (integer, `1..AKS_MAX_SCALE_NODE_COUNT` default 100,
  → 422 `invalid_node_count`), accepts an optional `auto_warmup` object mirroring
  `/aks/start`, enqueues `scale_aks`, invalidates the AKS monitor cache.
- **New Celery task** `api.tasks.azure.scale_aks`
  ([api/tasks/azure/lifecycle.py](../../api/tasks/azure/lifecycle.py)): resolves
  the workload pool (prefer `blastpool`, else first User-mode pool), skips the
  ARM PUT when the count is unchanged, otherwise PUTs the pool via
  `agent_pools.begin_create_or_update` and records an `aks_scale` lifecycle
  timing. Chains the re-warm through the new shared `_enqueue_forced_rewarm`
  helper with `num_nodes_override` pinned to the new size. The re-warm is also
  enqueued on the no-op branch when `auto_warmup` is supplied, closing a
  retry-after-PUT race where an autoretry could otherwise silently drop the
  re-warm (the re-warm is idempotent, so this is safe).
- **Refactor**: `start_aks`'s inline re-warm block was extracted into
  `_enqueue_forced_rewarm` (no behaviour change) so start and scale share one
  forced-reconcile path.
- **Frontend**: `aksApi.scale` ([web/src/api/aks.ts](../../web/src/api/aks.ts)),
  new `ScalePanel` + pure `scaleNodeCount` helpers
  ([web/src/components/ClusterItem/](../../web/src/components/ClusterItem/)),
  wired into `ClusterItem` using the workload-pool node count
  (`getWorkloadNodeCount`).
- No IaC changes (scaling is a runtime ARM operation on an existing pool).

## Validation evidence

- `uv run ruff check api` → All checks passed.
- `uv run pytest -q api/tests` → 3114 passed, 3 skipped.
  - New `scale_aks` tests in
    [api/tests/test_azure_tasks.py](../../api/tests/test_azure_tasks.py):
    resize + re-warm chaining (num_nodes pinned, force_rewarm_pending), no-op
    when unchanged, no-warmup skips reconcile, User-pool fallback, raises when no
    workload pool.
  - New route tests in
    [api/tests/test_warmup_route.py](../../api/tests/test_warmup_route.py):
    forwards node_count + auto_warmup; rejects invalid counts (0/-1/9999/abc/None)
    with 422 and no enqueue.
- `cd web && npx vitest run` → 740 passed (incl. new
  `scaleNodeCount.test.ts`, 6 cases).
- `cd web && npm run build` → built clean.

## Operational notes

- Scaling requires the cluster to be Running (ARM cannot resize a stopped
  cluster's pool); the UI hides the panel otherwise.
- Scale-down drains nodes via ARM; in-flight BLAST Jobs on a removed node are
  rescheduled by Kubernetes onto the remaining workload nodes.
- The re-warm is idempotent — the warm-up task skips already-cached nodes, so a
  scale-down (remaining nodes already warm) is a cheap reconcile.
