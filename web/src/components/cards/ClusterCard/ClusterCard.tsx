import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle } from "lucide-react";

import { monitoringApi } from "@/api/endpoints";
import { armProxyApi } from "@/api/armProxy";
import { aksApi } from "@/api/endpoints";
import { formatApiError } from "@/api/client";
import { ClusterItem } from "@/components/ClusterItem";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { MonitorCard } from "@/components/MonitorCard";
import { degradedStatusOverride } from "@/components/cards/cardStatusOverride";
import { useAksAvailableSkus, useAksSkus } from "@/hooks/useAksSkus";
import { useAutoRefreshInterval } from "@/hooks/useAutoRefresh";
import { isAksProvisioning, isAksProvisioningFailed } from "@/utils/aksStatus";
import { getDegradedInfo } from "@/utils/monitorDegraded";

import { AddClusterButton } from "./AddClusterButton";
import { ClusterListSkeleton } from "./ClusterListSkeleton";
import { ProvisionDoneBanner, ProvisioningBanner } from "./ProvisioningBanner";
import { ProvisionModal } from "./ProvisionModal";
import { ProvisionErrorCard } from "./ProvisionErrorCard";
import {
  dismissLastFailedProvision,
  loadDismissThreshold,
  loadLastFailedProvision,
  type LastFailedProvision,
} from "./lastFailedProvision";
import { useClusterActions } from "./useClusterActions";
import {
  nextElbClusterName,
  useClusterProvisioning,
} from "./useClusterProvisioning";

interface Props {
  subscriptionId: string;
  // Anchor RG kept in props for parent compatibility (storage / ACR / terminal
  // sidecar still derive workload defaults from it). The cluster list itself
  // is now subscription-wide — see queryKey below — so the card surfaces
  // every ElasticBLAST-managed cluster the caller can see regardless of
  // which RG hosts it.
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
  // Subscription-wide list — needs only the subscription id. The card no
  // longer hides clusters that live outside the dashboard's anchor RG, so
  // a multi-cluster deployment (heavy + light + gpu) renders inline.
  const enabled = Boolean(subscriptionId);
  const refetchInterval = useAutoRefreshInterval();
  const query = useQuery({
    queryKey: ["aks", subscriptionId, "sub"],
    // Empty RG arg => backend uses subscription-wide path with ElasticBLAST
    // tag filter (managedBy=elb-dashboard OR app=elastic-blast OR the legacy
    // blastpool+`workload=blast` taint fingerprint). Foreign clusters in
    // the same subscription are intentionally excluded.
    queryFn: () => monitoringApi.aks(subscriptionId),
    enabled,
    refetchInterval,
  });

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
  // P3-2: persist the last failed provision in localStorage so a
  // browser reload still surfaces a "Last attempt failed" banner
  // (with retry button) instead of dropping the error on the floor.
  // R-1: server-side source via /api/aks/recent-failed-provisions
  // takes precedence — it survives cross-browser sessions while
  // localStorage is per-browser. Hydrated once on mount; the server
  // query is best-effort (any error falls back to localStorage).
  // Cleared on Dismiss or when a new attempt succeeds (handled
  // inside `useClusterProvisioning`).
  const [lastFailed, setLastFailed] = useState<LastFailedProvision | null>(
    null,
  );
  useEffect(() => {
    let cancelled = false;
    // Localstorage hydration is synchronous and provides instant UI;
    // the async server fetch then overrides if it returns a newer
    // (or first) row.
    const fromLocal = loadLastFailedProvision();
    if (fromLocal) setLastFailed(fromLocal);
    if (!enabled) return;
    (async () => {
      try {
        const res = await aksApi.recentFailedProvisions(24, 1);
        if (cancelled) return;
        if (res.degraded || res.jobs.length === 0) return;
        const top = res.jobs[0];
        const whenMs = top.updated_at
          ? new Date(top.updated_at).getTime()
          : Date.now();
        // Dismiss threshold (set by Dismiss / Retry / Success) wins
        // over server hydration. Without this, a user can dismiss the
        // banner, reload the page, and the same server-side jobstate
        // row (24 h window) re-surfaces the banner — exactly the bug
        // the user reported.
        if (
          Number.isFinite(whenMs) &&
          whenMs <= loadDismissThreshold()
        )
          return;
        // Backend wins when it has a strictly-newer entry; otherwise
        // we keep the local snapshot so a recent in-browser failure
        // is not overwritten by a stale server row.
        if (
          !fromLocal ||
          (Number.isFinite(whenMs) && whenMs > fromLocal.when)
        ) {
          setLastFailed({
            raw: top.error_code ?? "Provisioning failed.",
            clusterName: top.cluster_name ?? "",
            region: top.region ?? "",
            resourceGroup: top.resource_group ?? "",
            subscriptionId: top.subscription_id ?? "",
            when: whenMs,
          });
        }
      } catch {
        // Server fetch is best-effort. localStorage source remains.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [enabled]);

  // R-2: cross-tab sync. The `storage` event fires on *other* tabs
  // when localStorage changes; this lets a failure in Tab A become
  // visible on Tab B without requiring a manual refresh. We re-read
  // the slot on any change to our key so add/clear both propagate.
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key !== "elb_last_failed_provision_v1" && e.key !== null) return;
      setLastFailed(loadLastFailedProvision());
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  // Stale-failure detection moves below, after `prov` is initialised, so it
  // can see clusters in *both* the dashboard's workload RG and the RG the
  // user just provisioned into (which may differ — modal RG defaults to
  // `rg-<clusterBase>`, see useClusterProvisioning).

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
    modalOpen: showProvision,
    query,
  });

  // Per-region SKU availability — used by the modal to grey out SKUs
  // the user's subscription cannot deploy in `prov.provisionRegion`.
  // Fetched lazily (only after the modal is opened) since the answer
  // is per-region and most users only ever try one or two.
  const availability = useAksAvailableSkus({
    subscriptionId,
    region: showProvision ? prov.provisionRegion : undefined,
    allSkus: skuOptions,
  });

  // Subscription-wide list — the backend filters to ElasticBLAST-managed
  // clusters by ARM tag (with the blastpool+taint legacy fallback) so we
  // can render the dashboard's full multi-cluster fleet without juggling
  // RG-scoped sub-queries. The reload-safe `recentAttempt` slot used to
  // exist purely to bridge "I just provisioned a cluster into a different
  // RG"; that gap is closed by the sub-wide list itself.
  const clusters = query.data?.clusters ?? [];
  const noClusters = clusters.length === 0;
  const hasProvisioningCluster = clusters.some(isAksProvisioning);
  const hasFailedProvisioningCluster = clusters.some(isAksProvisioningFailed);
  const clusterFetchHasSettled = query.dataUpdatedAt > 0 || query.errorUpdatedAt > 0;
  const showInitialClusterSkeleton =
    enabled && query.isLoading && !clusterFetchHasSettled;
  const showEmptyClusterState =
    enabled && !showInitialClusterSkeleton && !query.isError && noClusters;
  const showErrorClusterAction =
    enabled && !showInitialClusterSkeleton && query.isError && noClusters;

  // Stale-failure detection: once the cluster shows up in the list (any
  // `provisioning_state`) the lingering "Last attempt failed" banner is
  // redundant. We record a dismiss threshold so server hydration on the
  // next reload doesn't bring it back.
  const lastFailedIsStale = useMemo(() => {
    if (!lastFailed?.clusterName) return false;
    return clusters.some((c) => c.name === lastFailed.clusterName);
  }, [lastFailed, clusters]);
  useEffect(() => {
    if (!lastFailedIsStale) return;
    if (!lastFailed) return;
    dismissLastFailedProvision(lastFailed.when);
    setLastFailed(null);
  }, [lastFailedIsStale, lastFailed]);

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
    : showInitialClusterSkeleton
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
      subtitle={
        enabled
          ? `Subscription-wide${resourceGroup ? ` · anchor: ${resourceGroup}` : ""}`
          : "Configure subscription / RG"
      }
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
        enabled && !showInitialClusterSkeleton && !noClusters ? (
          <AddClusterButton variant="pill" onClick={openProvision} />
        ) : null
      }
      accentColor="cluster"
      collapsible
      loadingFallback={
        showInitialClusterSkeleton ? <ClusterListSkeleton /> : undefined
      }
    >
      {!enabled && (
        <div className="muted">Set Subscription ID and Workload RG above.</div>
      )}
      {query.isError && (
        <div className="muted" style={{ color: "var(--danger)" }}>
          Failed to load clusters: {formatApiError(query.error, "aks")}
        </div>
      )}
      {showErrorClusterAction && (
        <AddClusterButton variant="dashed" onClick={openProvision} />
      )}

      {showEmptyClusterState &&
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
          tier={prov.tier}
          setTier={prov.setTier}
          skuOptions={skuOptions}
          groupLabels={groupLabels}
          groupOrder={groupOrder}
          availableSkusSet={availability.availableSet}
          unavailableSkusMap={availability.unavailableMap}
          availabilityLoading={availability.isLoading || availability.isFetching}
          availabilityDegraded={availability.degraded}
          region={prov.provisionRegion}
          setRegion={prov.setProvisionRegion}
          availableLocations={availableLocations}
          locationsLoading={locationsQuery.isLoading}
          resourceGroup={prov.provisionResourceGroup}
          setResourceGroup={prov.setProvisionResourceGroup}
          resourceGroupValid={prov.provisionResourceGroupValid}
          resourceGroupExists={prov.provisionResourceGroupExists}
          resourceGroupsLoading={rgListQuery.isLoading}
          workloadResourceGroup={resourceGroup}
          preflightStatus={prov.preflightStatus}
          preflightResult={prov.preflightResult}
          taskPhase={prov.taskPhase}
          taskProgress={prov.taskProgress}
          elapsed={prov.elapsed}
          subscriptionId={subscriptionId}
          provStatus={prov.provStatus}
          provError={prov.provError}
          onSubmit={prov.handleProvision}
          onClose={() => setShowProvision(false)}
          onErrorReset={prov.resetError}
          onCancel={prov.cancelProvision}
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
          taskPhase={prov.taskPhase}
          taskProgress={prov.taskProgress}
          onCancel={prov.cancelProvision}
          targetResourceGroup={prov.provisionResourceGroup}
          targetRegion={prov.provisionRegion}
        />
      )}
      {prov.provStatus === "done" && (
        <ProvisionDoneBanner clusterName={prov.clusterName} roleResult={roleResult} />
      )}
      {prov.provError && !showProvision && (
        <div style={{ marginBottom: "var(--space-3)" }}>
          <ProvisionErrorCard
            raw={prov.provError}
            context={{
              subscriptionId,
              region: prov.provisionRegion,
              resourceGroup: prov.provisionResourceGroup,
            }}
            // R-3: if the task was cancelled (REVOKED) *after* the
            // cluster ARM resource was already visible, surface a
            // direct portal link so the user can verify and delete
            // the partial cluster.
            extraPortalUrl={
              prov.provError.includes("cancelled") &&
              typeof prov.taskProgress?.portal_url === "string"
                ? (prov.taskProgress?.portal_url as string)
                : undefined
            }
            onDismiss={prov.resetError}
            onRetry={() => {
              prov.resetError();
              setShowProvision(true);
            }}
          />
        </div>
      )}
      {/* P3-2: sticky "Last attempt failed" banner — only when the
          modal is closed and there is no live error already rendered
          above (we never want to show the same failure twice). Also
          suppressed when a retry has already landed a Succeeded cluster
          with the same name in this RG (stale record). */}
      {lastFailed && !lastFailedIsStale && !showProvision && !prov.provError && (
        <div style={{ marginBottom: "var(--space-3)" }}>
          <ProvisionErrorCard
            raw={lastFailed.raw}
            context={{
              subscriptionId: lastFailed.subscriptionId,
              region: lastFailed.region,
              resourceGroup: lastFailed.resourceGroup,
            }}
            onDismiss={() => {
              dismissLastFailedProvision(lastFailed.when);
              setLastFailed(null);
            }}
            onRetry={() => {
              prov.applyLastFailedContext({
                clusterName: lastFailed.clusterName,
                region: lastFailed.region,
                resourceGroup: lastFailed.resourceGroup,
              });
              dismissLastFailedProvision(lastFailed.when);
              setLastFailed(null);
              setShowProvision(true);
            }}
          />
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
        {clusters.map((c) => (
          <ClusterItem
            key={`${c.resource_group}/${c.name}`}
            cluster={c}
            transitioning={actions.transitioning}
            actionLoading={actions.actionLoading}
            onStartStop={actions.handleStartStop}
            onDelete={actions.setDeleteTarget}
            subscriptionId={subscriptionId}
            // Per-row RG so autoWarmup / start / stop / delete payloads
            // target the cluster's *actual* RG instead of the card's
            // anchor RG. Multi-cluster fleets (heavy + light) land in
            // different RGs by default; using the prop RG everywhere
            // would silently misroute the action to a non-existent
            // cluster name in the anchor RG.
            resourceGroup={c.resource_group}
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
      {showEmptyClusterState && (
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
