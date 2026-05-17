import { useEffect, useRef, useState } from "react";

import { monitoringApi, type AksClusterSummary } from "@/api/endpoints";
import { ClusterDetails } from "@/components/ClusterDetailModal";
import { ClusterBento } from "@/components/cards/ClusterBento";
import { DB_CATALOG } from "@/components/cards/storageDbCatalog";
import {
  AUTO_WARMUP_PREFS_EVENT,
  readAutoWarmupDbs,
} from "@/components/cards/storage/autoWarmupPrefs";
import { isAksWorkloadReady } from "@/utils/aksStatus";

import { ClusterHeaderBand } from "./ClusterHeaderBand";
import { ClusterStateRow } from "./ClusterStateRow";
import { DatabaseChipStrip } from "./DatabaseChipStrip";
import { PoolCardsGrid } from "./PoolCardsGrid";
import { ShardingCapacityRow } from "./ShardingCapacityRow";
import { useClusterActiveSubmissions } from "./useClusterActiveSubmissions";
import { useClusterDbChips } from "./useClusterDbChips";
import { useClusterShardMutation } from "./useClusterShardMutation";

const CLUSTER_COLLAPSED_KEY = "elb-cluster-collapsed-";
const CLUSTER_DETAILS_EXPANDED_KEY = "elb-cluster-details-expanded-";

// ClusterItem — collapsible per-cluster card (stopped clusters collapsed by default)
// ---------------------------------------------------------------------------

export function ClusterItem({
  cluster: c,
  transitioning,
  actionLoading,
  onStartStop,
  onDelete,
  subscriptionId,
  resourceGroup,
  storageAccount,
  storageResourceGroup,
  acrResourceGroup,
  acrName,
  region,
  terminalResourceGroup,
  terminalVmName,
}: {
  cluster: AksClusterSummary;
  transitioning: Map<string, "starting" | "stopping">;
  actionLoading: string | null;
  onStartStop: (name: string, action: "start" | "stop") => void;
  onDelete: (name: string) => void;
  subscriptionId: string;
  resourceGroup: string;
  storageAccount?: string;
  storageResourceGroup?: string;
  acrResourceGroup?: string;
  acrName?: string;
  region?: string;
  terminalResourceGroup?: string;
  terminalVmName?: string;
}) {
  const isStopped = c.power_state === "Stopped";
  const isRunning = isAksWorkloadReady(c);
  const trans = transitioning.get(c.name);
  const isTransitioning = transitioning.has(c.name);
  const showOperationalDetails = isRunning && !isTransitioning;

  const [collapsed, setCollapsed] = useState(() => {
    try {
      const v = localStorage.getItem(CLUSTER_COLLAPSED_KEY + c.name);
      return v != null ? v === "1" : isStopped;
    } catch {
      return isStopped;
    }
  });

  const toggleCollapse = () => {
    setCollapsed((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(CLUSTER_COLLAPSED_KEY + c.name, next ? "1" : "0");
      } catch {
        /* noop */
      }
      return next;
    });
  };

  // Whether the legacy "deep technical" rows (PoolCardsGrid +
  // DatabaseChipStrip + ClusterDetails) are visible below the bento.
  // Collapsed by default — the bento covers the dashboard summary
  // story; the deep rows are kept available for sharding actions and
  // node detail.
  const [detailsExpanded, setDetailsExpanded] = useState(() => {
    try {
      return localStorage.getItem(CLUSTER_DETAILS_EXPANDED_KEY + c.name) === "1";
    } catch {
      return false;
    }
  });
  const [autoWarmupDbs, setAutoWarmupDbs] = useState<Set<string>>(() =>
    readAutoWarmupDbs(),
  );
  const autoWarmupSyncKeyRef = useRef("");

  useEffect(() => {
    const refresh = () => setAutoWarmupDbs(readAutoWarmupDbs());
    window.addEventListener(AUTO_WARMUP_PREFS_EVENT, refresh);
    window.addEventListener("storage", refresh);
    return () => {
      window.removeEventListener(AUTO_WARMUP_PREFS_EVENT, refresh);
      window.removeEventListener("storage", refresh);
    };
  }, []);
  const toggleDetailsExpanded = () => {
    setDetailsExpanded((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(CLUSTER_DETAILS_EXPANDED_KEY + c.name, next ? "1" : "0");
      } catch {
        /* noop */
      }
      return next;
    });
  };

  const clusterNumNodes = c.node_count ?? 0;
  const clusterMachineType = c.node_sku ?? "";

  const { warmupQuery, warmupDbs, isWarm, dbChips, infeasibleDbs, dbListDegraded } =
    useClusterDbChips({
      subscriptionId,
      resourceGroup,
      clusterName: c.name,
      isRunning,
      isTransitioning,
      storageAccount,
      storageResourceGroup,
      clusterNumNodes,
      clusterMachineType,
    });

  useEffect(() => {
    if (!storageAccount) {
      return;
    }
    const databases = [...autoWarmupDbs].sort();
    const programs = Object.fromEntries(
      databases.map((dbName) => {
        const catalog = DB_CATALOG.find((item) => item.value === dbName);
        return [dbName, catalog?.type === "prot" ? "blastp" : "blastn"];
      }),
    );
    const payload = {
      subscription_id: subscriptionId,
      resource_group: resourceGroup,
      cluster_name: c.name,
      storage_account: storageAccount,
      storage_resource_group: storageResourceGroup || resourceGroup,
      region: region || c.region,
      databases,
      programs,
      enabled: databases.length > 0,
      acr_resource_group: acrResourceGroup,
      acr_name: acrName,
      terminal_resource_group: terminalResourceGroup,
      terminal_vm_name: terminalVmName,
      machine_type: c.node_sku || undefined,
      num_nodes: c.node_count || undefined,
    };
    const key = JSON.stringify(payload);
    if (autoWarmupSyncKeyRef.current === key) return;
    autoWarmupSyncKeyRef.current = key;
    void monitoringApi.saveAutoWarmupPreference(payload).catch(() => {
      autoWarmupSyncKeyRef.current = "";
    });
  }, [
    acrName,
    acrResourceGroup,
    autoWarmupDbs,
    c.name,
    c.node_count,
    c.node_sku,
    c.region,
    region,
    resourceGroup,
    storageAccount,
    storageResourceGroup,
    subscriptionId,
    terminalResourceGroup,
    terminalVmName,
  ]);

  const { shardMutation, shardError, shardingDb } = useClusterShardMutation({
    subscriptionId,
    storageAccount,
    storageResourceGroup,
  });

  const { tracking, submissions } = useClusterActiveSubmissions({
    clusterName: c.name,
    isRunning,
    isTransitioning,
  });

  return (
    // #1 — Header promotion: drop the nested glass-card surface and use a
    // flat panel with a clear header band so the cluster name reads as the
    // dominant heading rather than "another card inside a card".
    <li
      style={{
        padding: 0,
        background: "rgba(255, 255, 255, 0.025)",
        border: "1px solid var(--border-weak)",
        borderRadius: 10,
        overflow: "hidden",
      }}
    >
      <ClusterHeaderBand
        cluster={c}
        collapsed={collapsed}
        onToggleCollapse={toggleCollapse}
        trans={trans}
        isRunning={isRunning}
        isWarm={isWarm}
        warmupDbsCount={warmupDbs.length}
        actionLoading={actionLoading}
        onStartStop={onStartStop}
        onDelete={onDelete}
      />

      {!collapsed && (
        <div
          style={{
            padding: "12px 14px",
            display: "flex",
            flexDirection: "column",
            gap: 10,
          }}
        >
          {/*
            Bento — primary live view. Drives Submit pipeline / API
            health / CPU / Memory / Active jobs / Live activity from
            the api sidecar. Cells degrade independently when an
            upstream is unavailable so the bento itself never breaks
            the cluster card.
          */}
          <ClusterBento
            cluster={c}
            subscriptionId={subscriptionId}
            resourceGroup={resourceGroup}
            isRunning={showOperationalDetails}
            transition={trans}
            onOpenDetail={toggleDetailsExpanded}
            detailsExpanded={detailsExpanded}
          />

          {/* Sharding chips remain a primary action surface — keep visible. */}
          {showOperationalDetails && (dbChips.length > 0 || dbListDegraded) && (
            <DatabaseChipStrip
              dbChips={dbChips}
              infeasibleDbs={infeasibleDbs}
              dbListDegraded={dbListDegraded}
              shardMutation={shardMutation}
              shardingDb={shardingDb}
              shardError={shardError}
              clusterNumNodes={clusterNumNodes}
              clusterMachineType={clusterMachineType}
            />
          )}

          {/*
            "Deep technical" detail — collapsed by default. Toggled by
            the bento's "Open" button (which calls `onOpenDetail` →
            `toggleDetailsExpanded`).  Surfaces per-pool node breakdown,
            sharding capacity ceiling, and the existing modal opener.
          */}
          {showOperationalDetails && detailsExpanded && (
            <>
              {c.agent_pools && c.agent_pools.length > 0 && (
                <PoolCardsGrid agentPools={c.agent_pools} />
              )}

              {isRunning && c.agent_pools && c.agent_pools.length > 0 && (
                <ShardingCapacityRow
                  agentPools={c.agent_pools}
                  tracking={tracking}
                  submissions={submissions}
                />
              )}

              <ClusterStateRow provisioningState={c.provisioning_state} />

              <ClusterDetails
                clusterName={c.name}
                powerState={c.power_state}
                isTransitioning={!!trans}
                agentPools={c.agent_pools}
                fqdn={c.fqdn}
                networkPlugin={c.network_plugin}
                subscriptionId={subscriptionId}
                resourceGroup={resourceGroup}
                warmupDbs={warmupDbs}
                warmupQuery={warmupQuery}
                storageAccount={storageAccount}
                storageResourceGroup={storageResourceGroup}
                acrResourceGroup={acrResourceGroup}
                acrName={acrName}
                region={region}
                nodeSku={c.node_sku}
                nodeCount={c.node_count}
                terminalResourceGroup={terminalResourceGroup}
                terminalVmName={terminalVmName}
                kubeletObjectId={c.kubelet_object_id}
              />
            </>
          )}
        </div>
      )}
    </li>
  );
}
