import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle } from "lucide-react";

import { monitoringApi } from "@/api/endpoints";
import { armProxyApi } from "@/api/armProxy";
import { formatApiError } from "@/api/client";
import { ClusterItem } from "@/components/ClusterItem";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { MonitorCard } from "@/components/MonitorCard";
import { degradedStatusOverride } from "@/components/cards/cardStatusOverride";
import { useAksSkus } from "@/hooks/useAksSkus";
import { useAutoRefreshInterval } from "@/hooks/useAutoRefresh";
import { isAksProvisioning, isAksProvisioningFailed } from "@/utils/aksStatus";
import { getDegradedInfo } from "@/utils/monitorDegraded";

import { AddClusterButton } from "./AddClusterButton";
import { ClusterListSkeleton } from "./ClusterListSkeleton";
import { ProvisionDoneBanner, ProvisioningBanner } from "./ProvisioningBanner";
import { ProvisionModal } from "./ProvisionModal";
import { useClusterActions } from "./useClusterActions";
import {
  nextElbClusterName,
  useClusterProvisioning,
} from "./useClusterProvisioning";

interface Props {
  subscriptionId: string;
  resourceGroup: string;
  region?: string;
  acrResourceGroup?: string;
  acrName?: string;
  storageResourceGroup?: string;
  storageAccount?: string;
  terminalResourceGroup?: string;
  terminalVmName?: string;
}

export function ClusterCard({
  subscriptionId,
  resourceGroup,
  region,
  acrResourceGroup,
  acrName,
  storageResourceGroup,
  storageAccount,
  terminalResourceGroup,
  terminalVmName,
}: Props) {
  const enabled = Boolean(subscriptionId && resourceGroup);
  const refetchInterval = useAutoRefreshInterval();
  const query = useQuery({
    queryKey: ["aks", subscriptionId, resourceGroup],
    queryFn: () => monitoringApi.aks(subscriptionId, resourceGroup),
    enabled,
    refetchInterval,
  });

  const noClusters = query.data?.clusters.length === 0;
  const clusters = query.data?.clusters ?? [];
  const hasProvisioningCluster = clusters.some(isAksProvisioning);
  const hasFailedProvisioningCluster = clusters.some(isAksProvisioningFailed);

  // Existing resource group names — fed into the provision modal so the user
  // can be warned before they submit a duplicate name. Fetched lazily; the
  // SPA tolerates an empty list (the modal then skips the duplicate warning).
  const rgListQuery = useQuery({
    queryKey: ["arm", "resource-groups", subscriptionId],
    queryFn: () => armProxyApi.listResourceGroups(subscriptionId),
    enabled: Boolean(subscriptionId),
    staleTime: 60_000,
  });
  // Stable reference so downstream hooks that take this array as a dep
  // don't re-run on every parent render. The mapped array changes only when
  // the underlying query data changes.
  const existingResourceGroupNames = useMemo(
    () => rgListQuery.data?.map((g) => g.name) ?? [],
    [rgListQuery.data],
  );
  // Subscription-allowed regions. Falls back to the hard-coded AZURE_REGIONS
  // constant inside ProvisionModal if this list is empty (network error,
  // permissions, or first paint before the query resolves).
  const locationsQuery = useQuery({
    queryKey: ["arm", "locations", subscriptionId],
    queryFn: () => armProxyApi.listLocations(subscriptionId),
    enabled: Boolean(subscriptionId),
    // Subscription locations change very rarely; cache for 15 minutes.
    staleTime: 15 * 60_000,
  });
  const availableLocations = locationsQuery.data ?? [];

  // Role assignment result (shown after provision completes).
  const [roleResult] = useState<string[] | null>(null);
  const [showProvision, setShowProvision] = useState(false);

  const {
    skus: skuOptions,
    defaultSystemSku,
    groupLabels,
    groupOrder,
  } = useAksSkus({ enabled });

  const prov = useClusterProvisioning({
    subscriptionId,
    resourceGroup,
    region,
    acrResourceGroup,
    acrName,
    storageResourceGroup,
    storageAccount,
    defaultSystemSku,
    existingResourceGroupNames,
    closeModal: () => setShowProvision(false),
    query,
  });

  const actions = useClusterActions({
    subscriptionId,
    resourceGroup,
    query,
    storageAccount,
    storageResourceGroup,
    acrResourceGroup,
    acrName,
    region,
    terminalResourceGroup,
    terminalVmName,
  });

  const openProvision = () => {
    const suggested = nextElbClusterName(clusters, existingResourceGroupNames);
    // Order matters: reset RG tracking before setting cluster name so the
    // auto-sync useEffect inside the hook picks up the new name and writes
    // `rg-<name>` itself. Setting RG explicitly would flip the userTouched
    // flag and break the auto-sync on subsequent cluster-name edits.
    prov.resetProvisionResourceGroupTracking();
    prov.setClusterName(suggested);
    prov.resetProvisionRegionToDashboard();
    setShowProvision(true);
  };

  const status = !enabled
    ? "idle"
    : query.isLoading
      ? "loading"
      : query.isError
        ? "error"
        : noClusters
          ? "not-provisioned"
          : hasFailedProvisioningCluster
            ? "error"
            : hasProvisioningCluster
              ? "loading"
              : "ok";

  const degradedInfo = getDegradedInfo(query.data);
  const statusOverride = degradedStatusOverride(degradedInfo);

  return (
    <MonitorCard
      title="Azure Kubernetes Service Cluster"
      subtitle={enabled ? resourceGroup : "Configure subscription / RG"}
      status={prov.provStatus === "creating" ? "loading" : status}
      statusOverride={statusOverride}
      fetching={query.isFetching}
      lastRefreshed={query.dataUpdatedAt ? new Date(query.dataUpdatedAt) : null}
      onRefresh={() => {
        actions.setActionError(null);
        prov.setProvError(null);
        query.refetch();
      }}
      rightSlot={
        enabled && !query.isLoading && !noClusters ? (
          <AddClusterButton variant="pill" onClick={openProvision} />
        ) : null
      }
      accentColor="cluster"
      collapsible
    >
      {!enabled && (
        <div className="muted">Set Subscription ID and Workload RG above.</div>
      )}
      {query.isError && (
        <div className="muted" style={{ color: "var(--danger)" }}>
          Failed to load clusters: {formatApiError(query.error, "aks")}
        </div>
      )}

      {enabled && query.isLoading && <ClusterListSkeleton />}

      {query.data?.clusters.length === 0 &&
        prov.provStatus !== "creating" &&
        prov.provStatus !== "done" && (
          <div className="muted">
            No AKS clusters found. Click "+ Add Cluster" below to provision one.
          </div>
        )}

      {showProvision && (
        <ProvisionModal
          clusterName={prov.clusterName}
          setClusterName={prov.setClusterName}
          clusterNameValid={prov.clusterNameValid}
          nodeSku={prov.nodeSku}
          setNodeSku={prov.setNodeSku}
          nodeCount={prov.nodeCount}
          setNodeCount={prov.setNodeCount}
          systemVmSize={prov.systemVmSize}
          setSystemVmSize={prov.setSystemVmSize}
          systemNodeCount={prov.systemNodeCount}
          setSystemNodeCount={prov.setSystemNodeCount}
          skuOptions={skuOptions}
          groupLabels={groupLabels}
          groupOrder={groupOrder}
          region={prov.provisionRegion}
          setRegion={prov.setProvisionRegion}
          availableLocations={availableLocations}
          locationsLoading={locationsQuery.isLoading}
          resourceGroup={prov.provisionResourceGroup}
          setResourceGroup={prov.setProvisionResourceGroup}
          resourceGroupValid={prov.provisionResourceGroupValid}
          resourceGroupConflict={prov.provisionResourceGroupConflict}
          resourceGroupsLoading={rgListQuery.isLoading}
          provStatus={prov.provStatus}
          provError={prov.provError}
          onSubmit={prov.handleProvision}
          onClose={() => setShowProvision(false)}
        />
      )}

      {prov.provStatus === "creating" && (
        <ProvisioningBanner
          clusterName={prov.clusterName}
          elapsed={prov.elapsed}
          nodeCount={prov.nodeCount}
          nodeSku={prov.nodeSku}
          systemNodeCount={prov.systemNodeCount}
          systemVmSize={prov.systemVmSize}
        />
      )}
      {prov.provStatus === "done" && (
        <ProvisionDoneBanner clusterName={prov.clusterName} roleResult={roleResult} />
      )}
      {prov.provError && (
        <div
          style={{ fontSize: 12, color: "var(--danger)", marginBottom: "var(--space-3)" }}
        >
          <AlertTriangle size={12} style={{ verticalAlign: "middle" }} /> {prov.provError}
        </div>
      )}

      <ul
        style={{
          margin: 0,
          padding: 0,
          listStyle: "none",
          display: "grid",
          gap: "var(--space-2)",
        }}
      >
        {query.data?.clusters.map((c) => (
          <ClusterItem
            key={c.name}
            cluster={c}
            transitioning={actions.transitioning}
            actionLoading={actions.actionLoading}
            onStartStop={actions.handleStartStop}
            onDelete={actions.setDeleteTarget}
            subscriptionId={subscriptionId}
            resourceGroup={resourceGroup}
            storageAccount={storageAccount}
            storageResourceGroup={storageResourceGroup}
            acrResourceGroup={acrResourceGroup}
            acrName={acrName}
            region={region}
            terminalResourceGroup={terminalResourceGroup}
            terminalVmName={terminalVmName}
          />
        ))}
      </ul>

      {/* Big dashed "Add Cluster" CTA only when the list is empty. */}
      {enabled && !query.isLoading && noClusters && (
        <AddClusterButton variant="dashed" onClick={openProvision} />
      )}

      {actions.actionError && (
        <div
          style={{ marginTop: "var(--space-2)", fontSize: 11, color: "var(--danger)" }}
        >
          <AlertTriangle size={10} style={{ verticalAlign: "middle" }} />{" "}
          {actions.actionError}
        </div>
      )}

      {actions.deleteTarget && (
        <ConfirmDialog
          title={`Delete cluster "${actions.deleteTarget}"?`}
          message="This action is irreversible. The cluster and all its workloads will be permanently deleted."
          confirmLabel="Delete"
          onConfirm={() => actions.handleDelete(actions.deleteTarget!)}
          onCancel={() => actions.setDeleteTarget(null)}
        />
      )}
    </MonitorCard>
  );
}
