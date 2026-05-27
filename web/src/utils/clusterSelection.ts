import type { AksClusterSummary } from "@/api/monitoring";
import { getWorkloadNodeCount } from "@/pages/blastSubmit/computeEnvironment";
import { isAksWorkloadReady } from "@/utils/aksStatus";

export interface PickPreferredClusterOptions {
  /** Strongest signal — when the caller already has a cluster name in hand. */
  name?: string;
  /** RG hint (used only when no workload-ready cluster is found). */
  resourceGroup?: string;
  /** Prefer a cluster with at least one workload node before the array fallback. */
  requireNodes?: boolean;
}

/**
 * Pick the cluster a multi-cluster fleet should default to.
 *
 * Centralises the fallback chain that several pages duplicated and that all
 * shared the same blind spot: dropping to `clusters[0]` could land on a
 * Stopped cluster while a healthy peer existed. The order is:
 *
 *   1. Exact name match (when `opts.name` is provided).
 *   2. Any workload-ready cluster (Running + Succeeded) — the single most
 *      important signal for "which cluster can serve traffic right now".
 *   3. RG match (when `opts.resourceGroup` is provided) — used as a tie
 *      breaker when no cluster is workload-ready yet.
 *   4. Cluster with workload nodes (when `opts.requireNodes` is provided) —
 *      preserved from StorageCard's pre-existing logic for the warmup
 *      topology card.
 *   5. The first cluster in the list — last resort so the UI still has
 *      something to render.
 *
 * Returns `undefined` only when the list is empty.
 */
export function pickPreferredCluster(
  clusters: AksClusterSummary[],
  opts: PickPreferredClusterOptions = {},
): AksClusterSummary | undefined {
  if (clusters.length === 0) return undefined;
  return (
    (opts.name ? clusters.find((c) => c.name === opts.name) : undefined) ??
    clusters.find(isAksWorkloadReady) ??
    (opts.resourceGroup
      ? clusters.find((c) => c.resource_group === opts.resourceGroup)
      : undefined) ??
    (opts.requireNodes
      ? clusters.find((c) => (getWorkloadNodeCount(c) ?? 0) > 0)
      : undefined) ??
    clusters[0]
  );
}
