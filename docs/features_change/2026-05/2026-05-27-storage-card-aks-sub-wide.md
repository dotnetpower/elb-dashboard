# Subscription-wide AKS list rolled out to remaining card / hooks (fix "no cluster" false alarm)

## Motivation

After 2026-05-25's subscription-wide AKS migration ([2026-05-25-aks-subscription-wide-list.md](./2026-05-25-aks-subscription-wide-list.md)),
`ClusterCard` and the BLAST submit flow correctly list every ELB-managed
cluster in the subscription, regardless of which RG they live in. But
`StorageCard` was left calling `monitoringApi.aks(subscriptionId, resourceGroup)`
with the dashboard's anchor `workloadResourceGroup` — the BLAST workload
cluster typically lives in its own RG (e.g. `elasticblast-<user>`), not
the anchor RG, so the RG-scoped call returned an empty list.

That empty list flipped `clusterTopology.hasCluster` to `false`, which
made the BLAST DB Get confirm modal render
**"No AKS workload cluster has been created yet."** even when a healthy
cluster was running. Users with an existing cluster were forced through
the "Get DB before AKS is ready" warning every time.

## User-facing change

* The BLAST databases section in the Storage card now sees every
  ELB-managed cluster in the subscription. When at least one cluster
  exists, the pre-Get confirm modal is suppressed (or shows the
  node-count warning instead of the cluster-missing warning).
* Cluster preference order in `StorageCard`: exact name match
  (existing `clusterName` prop, default `"elb-cluster"`) → cluster in
  the same RG as the storage account's anchor RG (preserves the
  legacy single-RG layout) → any cluster with workload nodes → first
  cluster. This keeps the previous "right cluster wins" behaviour for
  RG-co-located deployments while unblocking cross-RG layouts.

## API / IaC diff summary

### Frontend
* [web/src/components/cards/StorageCard.tsx](../../../web/src/components/cards/StorageCard.tsx):
  * `clusterQuery` switched from
    `monitoringApi.aks(subscriptionId, resourceGroup)` to
    `monitoringApi.aks(subscriptionId)` with query key
    `["aks", subscriptionId, "sub"]` (matches `ClusterCard`'s key, so
    TanStack Query dedupes the request).
  * `enabled` for the AKS query loosened to `Boolean(subscriptionId)`
    so the topology hydrates even before `storageAccountName` /
    `workloadResourceGroup` resolve.
  * `clusterTopology` preference list extended with a "same RG as the
    storage anchor" fallback before "first cluster with workload nodes"
    so existing single-RG deployments still pick their co-located
    cluster.
  * `isLoading` heuristic switched from `enabled` (which mixed storage
    readiness into the AKS-loading signal) to `Boolean(subscriptionId)`
    so the cluster shimmer matches the cluster query, not the storage
    query.
* [web/src/pages/Dashboard/useGettingStartedReadiness.ts](../../../web/src/pages/Dashboard/useGettingStartedReadiness.ts):
  * `aksQuery` switched to `monitoringApi.aks(config.subscriptionId)`
    with key `["aks", config.subscriptionId, "sub"]`. The `hasCluster`
    probe — which gates whether the auto-popping Getting Started panel
    keeps saying "Create AKS" — now sees clusters in any RG, matching
    the user's actual deployment state. Same `enabled` gating (full
    config + not dismissed) is preserved.
* [web/src/hooks/useScopedBlastJobs.ts](../../../web/src/hooks/useScopedBlastJobs.ts):
  * Cluster auto-discovery rewritten to a sub-wide probe. When the
    caller does not pin `clusterName`, the hook now prefers a cluster
    co-located with the storage anchor RG before falling back to the
    first cluster, so the BLAST jobs list no longer empties out when
    the cluster lives in `elasticblast-<user>` instead of the anchor
    RG. `enabled` gate loosened to `Boolean(subscriptionId)` only
    (RG is irrelevant to the sub-wide call).

### Backend / IaC / Tests
No changes — the sub-wide route, filter contract, and tests landed on
2026-05-25.

## Validation evidence

* `cd web && npx tsc --noEmit` — clean for the touched files.
  Pre-existing TS6133 errors in `K8sPodsSection.tsx` are unrelated
  WIP from a separate change and tracked elsewhere.
* `cd web && npx eslint src/components/cards/StorageCard.tsx
  src/hooks/useScopedBlastJobs.ts
  src/pages/Dashboard/useGettingStartedReadiness.ts` — clean.
* `cd web && npm run build` — clean (existing chunk-size warning only).
* `cd web && npx vitest run src/components/cards/storage/BlastDbClusterConfirm.test.ts` — 3 / 3 pass.
* Manual: with an AKS cluster present in a non-anchor RG, the BLAST DB
  Get button no longer surfaces "No AKS workload cluster has been
  created yet." The pre-Get modal either disappears (cluster has
  workload nodes) or shows the node-count warning, matching the actual
  cluster state. The Getting Started panel no longer auto-pops "Create
  AKS" and the BLAST jobs list resolves a cluster name on first paint.
