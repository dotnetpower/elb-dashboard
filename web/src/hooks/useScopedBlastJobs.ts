import { useQuery } from "@tanstack/react-query";

import { blastApi, monitoringApi } from "@/api/endpoints";
import { loadSavedConfig } from "@/components/SetupWizard";
import { pickPreferredCluster } from "@/utils/clusterSelection";

export interface ScopedBlastJobsOptions {
  clusterName?: string;
  enabled?: boolean;
  refetchInterval?: number | false;
  /**
   * When true (default) and no explicit `clusterName` is given, discover the
   * fleet and pin the jobs query to a single preferred cluster. Per-cluster
   * cards want this so each tile shows only its own cluster's jobs.
   *
   * Set false for history / "latest job" views (Recent searches, the topbar
   * chip) that must list the caller's jobs across ALL clusters in the
   * subscription. Auto-pinning to `clusters[0]` when every cluster is Stopped
   * silently hid the user's recent jobs that ran on a peer cluster — the
   * Recent searches page showed only the alphabetically-first cluster's
   * (often stale) jobs and the topbar chip surfaced a stale "latest" job.
   */
  autoSelectCluster?: boolean;
}

export function useScopedBlastJobs(options: ScopedBlastJobsOptions = {}) {
  const enabled = options.enabled ?? true;
  const autoSelectCluster = options.autoSelectCluster ?? true;
  const savedConfig = loadSavedConfig();
  const subscriptionId = savedConfig?.subscriptionId ?? "";
  const resourceGroup = savedConfig?.workloadResourceGroup ?? "";
  const hasWorkspaceContext = Boolean(subscriptionId && resourceGroup);
  const needsClusterDiscovery = !options.clusterName && autoSelectCluster;

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
  const discoveredCluster = autoSelectCluster
    ? pickPreferredCluster(discoveredClusters, { resourceGroup })
    : undefined;
  const selectedClusterName =
    options.clusterName || discoveredCluster?.name || "";
  // The cluster's own RG (typically `rg-elb-cluster`) is what gets stored on
  // the job state row, NOT the dashboard's workspace RG (where Storage / ACR
  // live). Use the discovered cluster's RG for the jobs query so the backend
  // scope filter matches even when the user's workspace RG differs.
  const clusterResourceGroup =
    discoveredCluster?.resource_group || resourceGroup;
  const clusterScopeReady =
    !hasWorkspaceContext ||
    Boolean(options.clusterName) ||
    !autoSelectCluster ||
    clustersQuery.isFetched;
  // Subscription-only listing (no cluster pinned). Used by history / latest
  // views: the backend treats subscription_id alone as "all of this caller's
  // jobs across every cluster", which is exactly what Recent searches wants.
  const subscriptionScopedOnly = !selectedClusterName && !autoSelectCluster;
  // When sub-scoped we must NOT send a resource_group: job rows carry the
  // cluster's RG (rg-elb-cluster), so filtering by the workspace RG
  // (rg-elb-dashboard) would hide every job. Only the legacy auto-select
  // pre-discovery path keeps the cluster RG fallback.
  const queryResourceGroup = selectedClusterName
    ? undefined
    : subscriptionScopedOnly
      ? undefined
      : clusterResourceGroup;

  const jobsQuery = useQuery({
    queryKey: [
      "blast-jobs",
      subscriptionId,
      queryResourceGroup ?? "",
      selectedClusterName,
    ],
    queryFn: () =>
      blastApi.listJobs({
        subscriptionId,
        // Omit resource_group entirely when we have a cluster_name — the
        // backend treats cluster_name as the strongest scope key, and
        // passing the wrong RG would hide jobs whose row carries the
        // cluster RG. Sub-scoped history listing also omits the RG so it
        // sees jobs on every cluster. When no cluster is discovered yet in
        // auto-select mode we fall back to the cluster's RG (if known) so a
        // pre-discovery refetch can still see in-flight rows.
        resourceGroup: queryResourceGroup,
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
