import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, Info } from "lucide-react";

import { monitoringApi } from "@/api/endpoints";
import { armProxyApi } from "@/api/armProxy";
import {
  RESOURCE_GROUPS_STALE_MS,
  resourceGroupsQueryKey,
} from "@/api/resourceGroups";
import { formatApiError } from "@/api/client";
import { ClusterItem } from "@/components/ClusterItem";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { MonitorCard } from "@/components/MonitorCard";
import { permissionDeniedTooltip } from "@/components/PermissionGate";
import { degradedStatusOverride } from "@/components/cards/cardStatusOverride";
import { useAksAvailableSkus, useAksSkus } from "@/hooks/useAksSkus";
import { usePermissions } from "@/hooks/usePermissions";
import { useAutoRefreshInterval } from "@/hooks/useAutoRefresh";
import {
  isAksProvisioning,
  isAksProvisioningFailed,
  isAksWorkloadReady,
} from "@/utils/aksStatus";
import { getDegradedInfo } from "@/utils/monitorDegraded";

import { AddClusterButton } from "./AddClusterButton";
import { ClusterListSkeleton } from "./ClusterListSkeleton";
import { ProvisionDoneBanner, ProvisioningBanner } from "./ProvisioningBanner";
import { ProvisionModal } from "./ProvisionModal";
import { ProvisionErrorCard } from "./ProvisionErrorCard";
import { hasActiveClusterTransitions, useClusterActions } from "./useClusterActions";
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
  const baseRefetchInterval = useAutoRefreshInterval();
  // While a cluster is provisioning / starting / stopping we must keep
  // polling even if the dashboard tab is backgrounded, and refetch the
  // moment the tab regains focus. Otherwise the global QueryClient defaults
  // (`refetchIntervalInBackground: false`, `refetchOnWindowFocus: false` in
  // main.tsx) freeze the row at "Starting…" until the user manually reloads.
  //
  // The row's "Starting…" label is an *optimistic transition chip* held in
  // localStorage; it is only cleared once a poll observes the settled
  // `power_state=Running` + `provisioning_state=Succeeded` (see
  // `transitionTargetReached`). So chip removal depends entirely on the
  // cluster-list query actually refetching. Seeded from the persisted-
  // transition store so a reload mid-transition resumes hot polling before
  // the first response lands; an effect below keeps it in sync with
  // provisioning rows + live transitions. When nothing is in flight this
  // stays `false`, so idle dashboards keep the calm, cost-minimised posture.
  const [activePolling, setActivePolling] = useState<boolean>(() =>
    hasActiveClusterTransitions(subscriptionId, resourceGroup),
  );
  // Mirror of `activePolling` for the stable `queryFn` closure to read the
  // *current* hot state at call time (it must not be re-created every render).
  const activePollingRef = useRef(activePolling);
  // While in flight, force a fast 5 s poll (capped below the user's chosen
  // auto-refresh) so the optimistic chip clears within seconds of Azure
  // settling, instead of waiting out a 30–60 s interval. Idle keeps the
  // user's chosen cadence.
  const refetchInterval = activePolling
    ? Math.min(baseRefetchInterval, 5_000)
    : baseRefetchInterval;
  const query = useQuery({
    queryKey: ["aks", subscriptionId, "sub"],
    // Empty RG arg => backend uses subscription-wide path with ElasticBLAST
    // tag filter (managedBy=elb-dashboard OR app=elastic-blast OR the legacy
    // blastpool+`workload=blast` taint fingerprint). Foreign clusters in
    // the same subscription are intentionally excluded.
    //
    // `fresh` is read from persisted transitions at call time (not capture
    // time) so that while a start/stop is in flight every poll asks the
    // backend to bypass its 30 s monitor cache and re-query ARM. That is the
    // only cross-process-safe way to surface the settled `provisioning_state`
    // promptly: the lifecycle task runs in the `worker` sidecar and cannot
    // invalidate the `api` sidecar's in-process cache. Once the transition
    // chip clears, `fresh` falls back to false and normal caching resumes.
    // `fresh` bypasses the backend's 30 s monitor cache and re-queries ARM
    // synchronously. It is read at call time (not capture time) from the
    // persisted-transition store OR the current hot state, so any
    // provisioning cluster — including one started outside this browser
    // (portal / CLI) where no local transition was recorded — still gets the
    // settled `provisioning_state` the moment ARM flips, instead of waiting
    // out the cache TTL. The lifecycle task runs in the `worker` sidecar and
    // cannot invalidate the `api` sidecar's in-process cache, so this fresh
    // read is the only cross-process-safe path to authoritative state. Once
    // everything settles `activePolling` falls back to false and normal
    // caching resumes.
    queryFn: () =>
      monitoringApi.aks(subscriptionId, undefined, {
        fresh:
          hasActiveClusterTransitions(subscriptionId, resourceGroup) ||
          activePollingRef.current,
      }),
    enabled,
    refetchInterval,
    // Keep the interval ticking even when the tab is hidden, and force a
    // refetch on focus return, but only while something is in flight. With
    // `staleTime: 0` during that window the focus refetch is never skipped as
    // "still fresh", so returning to the tab always pulls the settled state.
    refetchIntervalInBackground: activePolling,
    refetchOnWindowFocus: activePolling,
    staleTime: activePolling ? 0 : undefined,
  });

  // Existing resource group names — fed into the provision modal so the user
  // can be warned before they submit a duplicate name. Fetched lazily; the
  // SPA tolerates an empty list (the modal then skips the duplicate warning).
  const rgListQuery = useQuery({
    queryKey: resourceGroupsQueryKey(subscriptionId),
    queryFn: () => armProxyApi.listResourceGroups(subscriptionId),
    enabled: Boolean(subscriptionId),
    staleTime: RESOURCE_GROUPS_STALE_MS,
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
  // Provision errors are intentionally transient: `prov.provError`
  // lives only in this component's React state, so it surfaces once
  // when the failure happens and disappears on browser refresh or
  // when the user clicks Dismiss. A previous iteration hydrated a
  // "Last attempt failed" banner from localStorage + a 24 h
  // server-side `recent-failed-provisions` window, but that made
  // stale errors re-appear on every reload (even after the cluster
  // had been cleanly deleted) which is not what users expect.

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
  // Memoise so the `?? []` fallback does not create a fresh array reference
  // on every render — otherwise downstream `useMemo` / `useEffect` blocks
  // keyed off `clusters` re-fire on every parent render.
  const rawClusters = useMemo(
    () => query.data?.clusters ?? [],
    [query.data?.clusters],
  );
  // Issues-first sort so a multi-cluster fleet surfaces failed /
  // transitioning rows above happy ones. Within a bucket, fall back to
  // alphabetical name for a stable visual order across polls.
  const clusters = useMemo(() => {
    const bucket = (c: (typeof rawClusters)[number]): number => {
      if (isAksProvisioningFailed(c)) return 0;
      if (isAksProvisioning(c)) return 1;
      if (c.power_state === "Stopped") return 2;
      if (!isAksWorkloadReady(c)) return 3;
      return 4;
    };
    return [...rawClusters].sort((a, b) => {
      const ba = bucket(a);
      const bb = bucket(b);
      if (ba !== bb) return ba - bb;
      return a.name.localeCompare(b.name);
    });
  }, [rawClusters]);
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

  // Aggregate fleet KPIs — single line shown above the cluster list so
  // the operator can read "2 clusters · 1 running · 1 stopped" without
  // counting rows.
  const fleetKpi = useMemo(() => {
    let running = 0;
    let stopped = 0;
    let provisioning = 0;
    let failed = 0;
    for (const c of clusters) {
      if (isAksProvisioningFailed(c)) failed += 1;
      else if (isAksProvisioning(c)) provisioning += 1;
      else if (c.power_state === "Stopped") stopped += 1;
      else if (isAksWorkloadReady(c)) running += 1;
    }
    return { total: clusters.length, running, stopped, provisioning, failed };
  }, [clusters]);

  // Dashboard-wide `/api/blast` request metrics — used to live PER
  // cluster row, which was misleading because the value is a single
  // process-local figure for the dashboard backend itself, not the
  // K8s API server. Lift it to one card-header strip so every cluster
  // row no longer fires the same request and the label is honest.
  const dashboardMetricsQuery = useQuery({
    queryKey: ["request-metrics-blast", 900],
    queryFn: () =>
      monitoringApi.requestMetrics({
        windowSeconds: 900,
        pathPrefix: "/api/blast",
        rpmBuckets: 60,
      }),
    enabled,
    staleTime: 25_000,
    refetchInterval: enabled ? 30_000 : false,
    retry: 0,
  });
  const dashboardP95 = dashboardMetricsQuery.data?.p95_ms ?? null;
  const dashboardErrors = dashboardMetricsQuery.data?.errors ?? 0;
  const dashboardMetricsDegraded = dashboardMetricsQuery.data?.degraded === true;

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

  // Drive `activePolling` from authoritative signals: any cluster ARM
  // `provisioning_state` still in a transient phase (Creating / Starting /
  // Stopping / Updating / Deleting) OR a live in-browser start/stop/delete
  // transition. This survives a page reload (provisioning is read from the
  // refreshed cluster list, transitions from localStorage) so an externally
  // started cluster or a post-reload create also keeps hot-polling. Settles
  // back to `false` once everything is steady, restoring the idle posture.
  // The ref is updated synchronously so the next `queryFn` call (which may
  // fire before the state-driven re-render) reads the current hot state.
  useEffect(() => {
    const hot = hasProvisioningCluster || actions.transitioning.size > 0;
    activePollingRef.current = hot;
    setActivePolling((prev) => (prev === hot ? prev : hot));
  }, [hasProvisioningCluster, actions.transitioning]);

  // Gate "Add Cluster" behind the caller's write capability at the
  // subscription scope: provisioning creates a new resource group + AKS
  // cluster, so a Reader must see the button disabled with a role tooltip
  // instead of clicking through to a silent 403 (mirrors the Start / Stop /
  // Delete gating in PulseActions). usePermissions falls back to
  // OPEN_PERMISSIONS while loading and stays open when `degraded` so a
  // transient ARM hiccup never locks a privileged operator out.
  const { permissions: addClusterPermissions } = usePermissions(subscriptionId);
  const addClusterWriteDenied =
    !addClusterPermissions.can_write && !addClusterPermissions.degraded;

  // Block opening a second provision while either (a) the caller lacks
  // write permission, (b) a cluster in the list is still creating (ARM
  // `provisioning_state` says so) or (c) the current submit's task is
  // still in flight in this browser.
  const addClusterDisabled =
    addClusterWriteDenied ||
    hasProvisioningCluster ||
    prov.provStatus === "creating";
  const addClusterDisabledReason = addClusterWriteDenied
    ? permissionDeniedTooltip("can_write", addClusterPermissions)
    : addClusterDisabled
      ? "A cluster is currently being provisioned. Wait for it to finish before adding another."
      : undefined;

  // Dedupe the live provision: while the ProvisioningBanner is actively
  // tracking a create, the same cluster also shows up as a `Creating` row in
  // the list with every metric blank (`—`). Hide that row so the banner is the
  // single source of truth for the in-flight cluster. The KPI line still
  // counts it under "N provisioning". Match on name (+ RG when known) and only
  // while the row is still provisioning, so a freshly-Ready cluster reappears.
  const visibleClusters = useMemo(() => {
    if (prov.provStatus !== "creating" || !prov.clusterName) return clusters;
    const trackedName = prov.clusterName;
    const trackedRg = prov.provisionResourceGroup;
    return clusters.filter(
      (c) =>
        !(
          c.name === trackedName &&
          (!trackedRg || c.resource_group === trackedRg) &&
          isAksProvisioning(c)
        ),
    );
  }, [clusters, prov.provStatus, prov.clusterName, prov.provisionResourceGroup]);

  const openProvision = () => {
    if (addClusterDisabled) return;
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
          ? resourceGroup
            ? `Workspace RG: ${resourceGroup} \u00b7 clusters listed subscription-wide`
            : "Clusters listed subscription-wide"
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
          <AddClusterButton
            variant="pill"
            onClick={openProvision}
            disabled={addClusterDisabled}
            disabledTitle={addClusterDisabledReason}
          />
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
        <AddClusterButton
          variant="dashed"
          onClick={openProvision}
          disabled={addClusterDisabled}
          disabledTitle={addClusterDisabledReason}
        />
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

      {enabled && !showInitialClusterSkeleton && clusters.length > 0 && (
        <div
          className="muted"
          style={{
            display: "flex",
            flexWrap: "wrap",
            alignItems: "baseline",
            gap: "4px 14px",
            marginBottom: "var(--space-2)",
            fontSize: 11,
            fontVariantNumeric: "tabular-nums",
            color: "var(--text-faint)",
          }}
        >
          <span>
            <strong style={{ color: "var(--text-secondary)" }}>
              {fleetKpi.total}
            </strong>{" "}
            cluster{fleetKpi.total === 1 ? "" : "s"}
          </span>
          {fleetKpi.running > 0 && (
            <span style={{ color: "var(--success)" }}>
              {fleetKpi.running} running
            </span>
          )}
          {fleetKpi.stopped > 0 && <span>{fleetKpi.stopped} stopped</span>}
          {fleetKpi.provisioning > 0 && (
            <span style={{ color: "var(--accent)" }}>
              {fleetKpi.provisioning} provisioning
            </span>
          )}
          {fleetKpi.failed > 0 && (
            <span style={{ color: "var(--danger)" }}>
              {fleetKpi.failed} failed
            </span>
          )}
          <span
            title="Dashboard backend /api/blast latency p95 and 5xx count over the last 15 minutes. Not a per-cluster signal."
            style={{ marginLeft: "auto", cursor: "help" }}
          >
            Control-plane API p95{" "}
            <strong style={{ color: "var(--text-secondary)" }}>
              {dashboardMetricsDegraded || dashboardP95 == null
                ? "\u2014"
                : dashboardP95 >= 1000
                  ? `${(dashboardP95 / 1000).toFixed(1)}s`
                  : `${Math.round(dashboardP95)}ms`}
            </strong>
            {" \u00b7 "}
            <span
              style={{
                color:
                  dashboardErrors > 0 ? "var(--danger)" : "var(--text-faint)",
              }}
            >
              {dashboardMetricsDegraded ? "\u2014" : dashboardErrors} 5xx / 15m
            </span>
          </span>
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
        {visibleClusters.map((c) => (
          <ClusterItem
            key={`${c.resource_group}/${c.name}`}
            cluster={c}
            transitioning={actions.transitioning}
            transitionStartedAt={actions.transitionStartedAt}
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
        <AddClusterButton
          variant="dashed"
          onClick={openProvision}
          disabled={addClusterDisabled}
          disabledTitle={addClusterDisabledReason}
        />
      )}

      {/* Live region so screen-reader users receive Start / Stop / Delete
          completion + error messages without polling the visible text. */}
      <div
        aria-live="polite"
        aria-atomic="true"
        style={{
          position: "absolute",
          width: 1,
          height: 1,
          padding: 0,
          margin: -1,
          overflow: "hidden",
          clip: "rect(0,0,0,0)",
          whiteSpace: "nowrap",
          border: 0,
        }}
      >
        {actions.actionError || actions.actionInfo || ""}
      </div>

      {actions.actionError && (
        <div
          role="status"
          style={{ marginTop: "var(--space-2)", fontSize: 11, color: "var(--danger)" }}
        >
          <AlertTriangle size={10} style={{ verticalAlign: "middle" }} />{" "}
          {actions.actionError}
        </div>
      )}

      {actions.actionInfo && (
        <div
          role="status"
          style={{ marginTop: "var(--space-2)", fontSize: 11, color: "var(--success, #16a34a)" }}
        >
          <Info size={10} style={{ verticalAlign: "middle" }} />{" "}
          {actions.actionInfo}
        </div>
      )}

      {actions.deleteTarget && (
        <ConfirmDialog
          title={`Delete cluster "${actions.deleteTarget}"?`}
          message="This action is irreversible. The cluster and all its workloads will be permanently deleted."
          confirmLabel="Delete"
          typeToConfirm={actions.deleteTarget}
          typeToConfirmLabel={`Type "${actions.deleteTarget}" to confirm`}
          onConfirm={() => actions.handleDelete(actions.deleteTarget!)}
          onCancel={() => actions.setDeleteTarget(null)}
        />
      )}
    </MonitorCard>
  );
}
