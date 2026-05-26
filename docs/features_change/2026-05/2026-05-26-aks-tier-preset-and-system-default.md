# AKS Create modal: tier presets + system pool default 1 node

## Motivation
- "Cluster classification (optional)" dropdown wrote only the `elb-tier`
  ARM tag. Option labels hinted at workload sizing ("heavy — large BLAST
  jobs", "light — quick smoke"), but selecting them had **no effect** on
  `nodeSku` / `nodeCount`. Users assumed the tier was a preset and were
  silently confused when their cluster came up on default E16s_v5 × 10.
- System pool defaulted to 2 nodes. For the typical single-cluster
  dashboard user this is wasted spend — 1 node is enough for CoreDNS /
  metrics-server in dev and test clusters.

## User-facing change
- Cluster classification dropdown:
  - `gpu` option removed (the SKU catalog has no GPU entries; gating UI
    on a missing path was misleading).
  - `light` / `general` / `heavy` now pre-fill the workload pool with
    `D16s_v3 × 2`, `E16s_v5 × 5`, `E32s_v5 × 10` respectively.
  - Option labels show the resolved preset (e.g. `heavy — E32s_v5 × 10`).
  - If the user has already edited the SKU or node count, selecting a
    tier preserves their values (touched-flag pattern, same as the
    region / RG fields).
  - Help text under the dropdown updated to describe the preset behavior.
- System pool default node count is now **1** (was 2). Help text under
  the node-count input notes that 2+ is recommended for higher
  availability of system add-ons in production-grade clusters.

## API / IaC diff summary
- `api/services/aks_skus.py`: `DEFAULT_SYSTEM_NODE_COUNT = 1` (was 2).
  This is the single source of truth surfaced by `GET /api/aks/skus`
  (`default_system_node_count`), consumed by the SPA as its fallback.
- `web/src/hooks/useAksSkus.ts`: `DEFAULT_AKS_SYSTEM_NODE_COUNT = 1`
  (matching FE fallback).
- `web/src/components/cards/ClusterCard/useClusterProvisioning.ts`:
  - `ClusterTier` no longer includes `"gpu"`.
  - New `CLUSTER_TIER_PRESETS` constant maps tier → `{ sku, nodes }`.
  - `setTier` wrapper applies the preset to `nodeSku` / `nodeCount`
    unless those have been user-touched.
  - New internal `nodeSkuUserTouched` / `nodeCountUserTouched` flags
    (set when the public `setNodeSku` / `setNodeCount` are called).
- `web/src/components/cards/ClusterCard/ProvisionModal.tsx`: updated
  classification dropdown help text + added a system-pool node-count
  hint. No structural change.
- No backend route / contract change; no Bicep change; no Celery task
  change.

## Validation
- `uv run pytest -q api/tests` → **1499 passed**.
- `cd web && npm run build` → clean build, no TS errors.
- No DB / IaC `what-if` needed (constant-only backend change; the
  Container App template is unchanged).
