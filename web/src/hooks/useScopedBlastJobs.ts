import { useQuery } from "@tanstack/react-query";

import { blastApi, monitoringApi } from "@/api/endpoints";
import { loadSavedConfig } from "@/components/SetupWizard";

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
  const discoveredCluster =
    discoveredClusters.find((cluster) => cluster.resource_group === resourceGroup) ??
    discoveredClusters[0];
  const selectedClusterName =
    options.clusterName || discoveredCluster?.name || "";
  const clusterScopeReady =
    !hasWorkspaceContext || Boolean(options.clusterName) || clustersQuery.isFetched;

  const jobsQuery = useQuery({
    queryKey: ["blast-jobs", subscriptionId, resourceGroup, selectedClusterName],
    queryFn: () =>
      blastApi.listJobs({
        subscriptionId,
        resourceGroup,
        clusterName: selectedClusterName,
      }),
    enabled: enabled && clusterScopeReady,
    refetchInterval: options.refetchInterval,
  });

  return {
    jobsQuery,
    clustersQuery,
    subscriptionId,
    resourceGroup,
    clusterName: selectedClusterName,
  } as const;
}
