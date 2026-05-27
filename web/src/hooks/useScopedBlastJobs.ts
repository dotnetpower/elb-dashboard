import { useQuery } from "@tanstack/react-query";

import { blastApi, monitoringApi } from "@/api/endpoints";
import { loadSavedConfig } from "@/components/SetupWizard";
import { pickPreferredCluster } from "@/utils/clusterSelection";

export interface ScopedBlastJobsOptions {
  clusterName?: string;
  enabled?: boolean;
  refetchInterval?: number | false;
}

export function useScopedBlastJobs(options: ScopedBlastJobsOptions = {}) {
  const enabled = options.enabled ?? true;
  const savedConfig = loadSavedConfig();
  const subscriptionId = savedConfig?.subscriptionId ?? "";
  const resourceGroup = savedConfig?.workloadResourceGroup ?? "";
  const hasWorkspaceContext = Boolean(subscriptionId && resourceGroup);
  const needsClusterDiscovery = !options.clusterName;

  // Subscription-wide cluster discovery (matches ClusterCard / StorageCard).
  // An RG-scoped probe missed clusters that elastic-blast had created in its
  // own RG, leaving the BLAST jobs list empty even though a cluster existed.
  const clustersQuery = useQuery({
    queryKey: ["aks", subscriptionId, "sub"],
    queryFn: () => monitoringApi.aks(subscriptionId),
    enabled: enabled && Boolean(subscriptionId) && needsClusterDiscovery,
    staleTime: 30_000,
  });

  const discoveredClusters = clustersQuery.data?.clusters ?? [];
  // Prefer a workload-ready cluster over the workspace-anchor RG match.
  // The previous fallback (`find(rg match) ?? clusters[0]`) could pick a
  // Stopped peer, and BLAST job rows are keyed by cluster_name \u2014 jobs
  // running on a different cluster in the fleet would silently vanish from
  // the list.
  const discoveredCluster = pickPreferredCluster(discoveredClusters, {
    resourceGroup,
  });
  const selectedClusterName =
    options.clusterName || discoveredCluster?.name || "";
  // The cluster's own RG (typically `rg-elb-cluster`) is what gets stored on
  // the job state row, NOT the dashboard's workspace RG (where Storage / ACR
  // live). Use the discovered cluster's RG for the jobs query so the backend
  // scope filter matches even when the user's workspace RG differs.
  const clusterResourceGroup =
    discoveredCluster?.resource_group || resourceGroup;
  const clusterScopeReady =
    !hasWorkspaceContext || Boolean(options.clusterName) || clustersQuery.isFetched;

  const jobsQuery = useQuery({
    queryKey: [
      "blast-jobs",
      subscriptionId,
      clusterResourceGroup,
      selectedClusterName,
    ],
    queryFn: () =>
      blastApi.listJobs({
        subscriptionId,
        // Omit resource_group entirely when we have a cluster_name — the
        // backend treats cluster_name as the strongest scope key, and
        // passing the wrong RG would hide jobs whose row carries the
        // cluster RG. When no cluster is discovered yet we fall back to
        // the cluster's RG (if known) so a pre-discovery refetch can
        // still see in-flight rows.
        resourceGroup: selectedClusterName ? undefined : clusterResourceGroup,
        clusterName: selectedClusterName,
      }),
    enabled: enabled && clusterScopeReady,
    refetchInterval: options.refetchInterval,
  });

  return {
    jobsQuery,
    clustersQuery,
    subscriptionId,
    resourceGroup: clusterResourceGroup,
    clusterName: selectedClusterName,
  } as const;
}
