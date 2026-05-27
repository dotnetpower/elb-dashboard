import type { AksClusterSummary } from "@/api/endpoints";
import { pickPreferredCluster } from "@/utils/clusterSelection";

export interface ApiReferenceClusterContext {
  /** The cluster that should drive every OpenAPI lookup on this page.
   *  May be undefined while the sub-wide AKS list is still loading. */
  cluster: AksClusterSummary | undefined;
  clusterName: string;
  resourceGroup: string;
  /** Full candidate list, sorted issues-first so a fleet-aware picker
   *  can render them in a stable order. Empty until the sub-wide AKS
   *  list resolves. */
  candidates: AksClusterSummary[];
}

/**
 * Pick which AKS cluster the API Reference page should target.
 *
 * Multi-cluster fleets used to land on `clusters[0]`, which is the
 * order Azure returns. With one running + one stopped cluster that
 * often meant the page rendered "AKS cluster is stopped" even though
 * a healthy peer existed.
 *
 * Preference order:
 *   1. `preferredClusterName` — the user's explicit pick (persisted
 *      across navigations via localStorage).
 *   2. Any workload-ready cluster (Running + Succeeded) via the
 *      shared `pickPreferredCluster` helper.
 *   3. `anchorResourceGroup` match — used as a tie breaker.
 *   4. `clusters[0]` — last resort so the page still has something to
 *      render (e.g. when every cluster is stopped).
 */
export function resolveApiReferenceClusterContext({
  clusters,
  anchorResourceGroup,
  preferredClusterName,
}: {
  clusters: AksClusterSummary[];
  anchorResourceGroup: string;
  preferredClusterName?: string;
}): ApiReferenceClusterContext {
  const cluster = pickPreferredCluster(clusters, {
    name: preferredClusterName,
    resourceGroup: anchorResourceGroup,
  });
  return {
    cluster,
    clusterName: cluster?.name ?? "",
    resourceGroup: cluster?.resource_group ?? anchorResourceGroup,
    candidates: clusters,
  };
}