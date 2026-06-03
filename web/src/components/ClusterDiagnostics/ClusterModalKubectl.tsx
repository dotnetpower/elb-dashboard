import { useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";

import { monitoringApi } from "@/api/endpoints";
import type { K8sNodeMetrics } from "@/api/endpoints";

import { CustomCommandPanel } from "./CustomCommandPanel";
import { K8sNodesSection } from "./K8sNodesSection";
import { K8sWorkloadsSection } from "./K8sWorkloadsSection";
import { NodeResourcesSection } from "./NodeResourcesSection";

/**
 * Composes the cluster-diagnostics drawer that lives inside the cluster
 * detail modal. Owns only the data-fetching wiring; each section file
 * owns its own rendering.
 *
 * The `topQuery` for node metrics is passed in by the parent so the
 * existing dashboard polling loop can stay the source of truth — this
 * file just adds the node query plus the Workloads card (Pods /
 * Deployments / Jobs), which owns its own lazy queries. "Refresh All"
 * refetches node-level data and invalidates the live Workloads tab.
 */
export interface ClusterModalKubectlProps {
  subscriptionId: string;
  resourceGroup: string;
  clusterName: string;
  topQuery: {
    isLoading: boolean;
    isFetching: boolean;
    isError: boolean;
    data?: { nodes: K8sNodeMetrics[] } | null;
    error?: unknown;
    refetch: () => void;
  };
}

export function ClusterModalKubectl({
  subscriptionId,
  resourceGroup,
  clusterName,
  topQuery,
}: ClusterModalKubectlProps) {
  const queryClient = useQueryClient();
  const nodesQuery = useQuery({
    queryKey: ["aks-nodes-fast", subscriptionId, resourceGroup, clusterName],
    queryFn: () => monitoringApi.k8sNodes(subscriptionId, resourceGroup, clusterName),
    staleTime: 60_000,
    retry: 1,
  });

  // `topQuery` is a UseQueryResult passed in from the parent. Reading
  // `isFetching` (not just `isLoading`) is what surfaces refresh activity —
  // refetch otherwise updates data silently and looks like "nothing happened"
  // when the numbers are steady.
  const isRefreshing = topQuery.isFetching || nodesQuery.isFetching;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div
        style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}
      >
        <div
          style={{
            fontSize: 11,
            fontWeight: 600,
            display: "flex",
            alignItems: "center",
            gap: 6,
          }}
        >
          <span
            style={{ width: 3, height: 14, borderRadius: 2, background: "var(--teal)" }}
          />
          Cluster Diagnostics
        </div>
        <button
          className="glass-button"
          onClick={() => {
            topQuery.refetch();
            nodesQuery.refetch();
            // The Workloads card owns its own lazy queries; invalidate the
            // shared prefix so the currently-live tab refetches too.
            queryClient.invalidateQueries({ queryKey: ["aks-workload"] });
          }}
          disabled={isRefreshing}
          style={{
            padding: "4px 10px",
            fontSize: 10,
            display: "flex",
            alignItems: "center",
            gap: 4,
            cursor: isRefreshing ? "wait" : "pointer",
          }}
          title={isRefreshing ? "Refreshing…" : "Refresh all diagnostics"}
        >
          <RefreshCw
            size={10}
            strokeWidth={1.5}
            className={isRefreshing ? "spin" : undefined}
          />
          {isRefreshing ? "Refreshing…" : "Refresh All"}
        </button>
      </div>

      <NodeResourcesSection query={topQuery} />
      <K8sNodesSection query={nodesQuery} />
      <K8sWorkloadsSection
        subscriptionId={subscriptionId}
        resourceGroup={resourceGroup}
        clusterName={clusterName}
      />
      <CustomCommandPanel
        subscriptionId={subscriptionId}
        resourceGroup={resourceGroup}
        clusterName={clusterName}
      />
    </div>
  );
}
