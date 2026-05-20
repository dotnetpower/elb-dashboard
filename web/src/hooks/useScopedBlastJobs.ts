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

  const clustersQuery = useQuery({
    queryKey: ["aks", subscriptionId, resourceGroup],
    queryFn: () => monitoringApi.aks(subscriptionId, resourceGroup),
    enabled: enabled && hasWorkspaceContext,
    staleTime: 30_000,
  });

  const selectedClusterName =
    options.clusterName || clustersQuery.data?.clusters?.[0]?.name || "";
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
