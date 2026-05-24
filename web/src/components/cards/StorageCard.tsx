import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { formatApiError } from "@/api/client";
import { monitoringApi } from "@/api/endpoints";
import { MonitorCard } from "@/components/MonitorCard";
import { degradedStatusOverride } from "@/components/cards/cardStatusOverride";
import type { BlastDbClusterTopology } from "@/components/cards/storage/BlastDbClusterConfirm";
import { BlastDbSection } from "@/components/cards/storage/BlastDbSection";
import { StorageContainersTable } from "@/components/cards/storage/StorageContainersTable";
import { StorageMetaGrid } from "@/components/cards/storage/StorageMetaGrid";
import { StorageWarnings } from "@/components/cards/storage/StorageWarnings";
import { useAutoRefreshInterval } from "@/hooks/useAutoRefresh";
import { getWorkloadNodeCount } from "@/pages/blastSubmit/computeEnvironment";
import { getDegradedInfo } from "@/utils/monitorDegraded";

interface Props {
  subscriptionId: string;
  resourceGroup: string;
  accountName: string;
  clusterName?: string;
  acrName?: string;
}

/**
 * Storage Account monitoring card. Composes:
 *   - meta grid (region/SKU/HNS/public access)
 *   - container list
 *   - BLAST databases section (separate sub-tree with its own modal + state)
 *
 * Lifecycle and per-section state live in their own modules; this file is the
 * coordinator that wires the storage query to its presentational parts.
 */
export function StorageCard({
  subscriptionId,
  resourceGroup,
  accountName,
  clusterName,
  acrName,
}: Props) {
  const enabled = Boolean(subscriptionId && resourceGroup && accountName);
  const refetchInterval = useAutoRefreshInterval();

  const query = useQuery({
    queryKey: ["storage", subscriptionId, resourceGroup, accountName],
    queryFn: () => monitoringApi.storage(subscriptionId, resourceGroup, accountName),
    enabled,
    refetchInterval,
  });

  const clusterQuery = useQuery({
    queryKey: ["aks", subscriptionId, resourceGroup],
    queryFn: () => monitoringApi.aks(subscriptionId, resourceGroup),
    enabled,
    refetchInterval,
  });

  const clusterTopology = useMemo<BlastDbClusterTopology>(() => {
    const clusters = clusterQuery.data?.clusters ?? [];
    const preferredCluster =
      clusters.find((cluster) => cluster.name === clusterName) ??
      clusters.find((cluster) => (getWorkloadNodeCount(cluster) ?? 0) > 0) ??
      clusters[0];
    return {
      hasCluster: clusterQuery.data ? clusters.length > 0 : null,
      clusterName: preferredCluster?.name ?? clusterName ?? null,
      nodeCount: preferredCluster ? getWorkloadNodeCount(preferredCluster) : null,
      isLoading:
        clusterQuery.isLoading ||
        (enabled && !clusterQuery.data && clusterQuery.isFetching),
      isError: clusterQuery.isError,
    };
  }, [
    clusterName,
    clusterQuery.data,
    clusterQuery.isError,
    clusterQuery.isFetching,
    clusterQuery.isLoading,
    enabled,
  ]);

  // Tracks whether BlastDbSection has any in-flight download — used only to
  // keep the card's "fetching" shimmer on while a copy is running.
  const [dbDownloading, setDbDownloading] = useState<string | null>(null);

  const status = !enabled
    ? "idle"
    : query.isLoading
      ? "loading"
      : query.isError
        ? "error"
        : "ok";
  const publicAccess = query.data?.public_network_access ?? null;
  const isPublic = publicAccess === "Enabled";
  const isHnsEnabled = query.data?.is_hns_enabled ?? null;

  const degradedInfo = getDegradedInfo(query.data);
  const statusOverride = degradedStatusOverride(degradedInfo);

  return (
    <MonitorCard
      title="Storage Account"
      subtitle={
        enabled ? (
          <>
            {accountName}
            <span className="storage-subtitle-rg"> · {resourceGroup}</span>
          </>
        ) : (
          "Configure account name"
        )
      }
      status={status}
      statusOverride={statusOverride}
      fetching={query.isFetching || dbDownloading !== null}
      lastRefreshed={query.dataUpdatedAt ? new Date(query.dataUpdatedAt) : null}
      onRefresh={() => query.refetch()}
      accentColor="storage"
      collapsible
    >
      {!enabled && (
        <div className="muted">
          Set Subscription ID, Workload RG, and Storage Account above.
        </div>
      )}
      {query.isError && (
        <div className="muted" style={{ color: "var(--danger)" }}>
          Failed to load storage: {formatApiError(query.error, "storage")}
        </div>
      )}
      {query.data && (
        <>
          <StorageWarnings isPublic={isPublic} isHnsEnabled={isHnsEnabled} />
          <StorageMetaGrid
            region={query.data.region}
            sku={query.data.sku}
            isHnsEnabled={isHnsEnabled}
            isPublic={isPublic}
          />
          <StorageContainersTable containers={query.data.containers} />
          <BlastDbSection
            subscriptionId={subscriptionId}
            resourceGroup={resourceGroup}
            accountName={accountName}
            clusterName={clusterName ?? "elb-cluster"}
            acrName={acrName}
            clusterTopology={clusterTopology}
            onDownloadingChange={setDbDownloading}
          />
        </>
      )}
    </MonitorCard>
  );
}
