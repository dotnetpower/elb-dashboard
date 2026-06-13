import { useEffect, useRef, useState } from "react";

import { monitoringApi, type AksClusterSummary } from "@/api/endpoints";
import { formatApiError } from "@/api/client";
import { useToast } from "@/components/Toast";
import { ClusterDetails } from "@/components/ClusterDetailModal";
import { ClusterPulse } from "@/components/cards/ClusterPulse";
import { DB_CATALOG } from "@/components/cards/storageDbCatalog";
import {
  AUTO_WARMUP_PREFS_EVENT,
  readAutoWarmupDbs,
} from "@/components/cards/storage/autoWarmupPrefs";
import { isAksWorkloadReady } from "@/utils/aksStatus";

import { DatabaseChipStrip } from "./DatabaseChipStrip";
import { StartEstimatePanel } from "./StartEstimatePanel";
import { startingStatusLine, useStartProgress } from "./startEstimate";
import { AutoStopPanel } from "./AutoStopPanel";
import { useClusterDbChips } from "./useClusterDbChips";
import { useClusterShardMutation } from "./useClusterShardMutation";
import type { ClusterTransitionKind } from "@/components/cards/ClusterCard/useClusterActions";

// ClusterItem — per-cluster row, driven by <ClusterPulse>.
//
// The card itself is now intentionally light: it owns the data hooks
// (DB chips, sharding mutation, auto-warmup sync) and the modal state,
// and hands a single-line pulse component the signals it needs to
// render the row. Old surfaces (`ClusterHeaderBand`, `ClusterBento`,
// `PoolCardsGrid`, `ShardingCapacityRow`, `ClusterStateRow`) were
// retired — their information moved into the pulse expansion (meta +
// jobs) and the existing detail modal.
// ---------------------------------------------------------------------------

export function ClusterItem({
  cluster: c,
  transitioning,
  transitionStartedAt,
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
  transitioning: Map<string, ClusterTransitionKind>;
  transitionStartedAt: Map<string, number>;
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
  const isRunning = isAksWorkloadReady(c);
  const trans = transitioning.get(c.name);
  const isTransitioning = transitioning.has(c.name);
  const showOperationalDetails = isRunning && !isTransitioning;
  // The start timestamp lives in the persisted transition record (owned by
  // useClusterActions), so the elapsed clock keeps counting from the real
  // start across page reloads instead of resetting to 0 on every refresh.
  const startedAt = trans === "starting" ? (transitionStartedAt.get(c.name) ?? null) : null;
  const [detailOpen, setDetailOpen] = useState(false);

  const [autoWarmupDbs, setAutoWarmupDbs] = useState<Set<string>>(() =>
    readAutoWarmupDbs(),
  );
  const autoWarmupSyncKeyRef = useRef("");
  // Track the payload key whose save last errored so a persistent backend
  // failure surfaces exactly ONE toast per distinct preference change rather
  // than re-toasting on every dependency-driven re-run of the sync effect.
  const autoWarmupErrorKeyRef = useRef("");
  const { toast } = useToast();

  // Live timing model for the "Starting…" state — shared by the always-visible
  // status line and the expanded StartEstimatePanel so they never disagree.
  const startProgress = useStartProgress({
    startedAt,
    autoWarmupDbCount: autoWarmupDbs.size,
  });
  const startingLine =
    trans === "starting"
      ? startingStatusLine(startProgress, autoWarmupDbs.size)
      : undefined;

  useEffect(() => {
    const refresh = () => setAutoWarmupDbs(readAutoWarmupDbs());
    window.addEventListener(AUTO_WARMUP_PREFS_EVENT, refresh);
    window.addEventListener("storage", refresh);
    return () => {
      window.removeEventListener(AUTO_WARMUP_PREFS_EVENT, refresh);
      window.removeEventListener("storage", refresh);
    };
  }, []);
  const clusterNumNodes = c.node_count ?? 0;
  const clusterMachineType = c.node_sku ?? "";

  const { warmupQuery, warmupDbs, dbChips, infeasibleDbs, dbListDegraded } =
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
    void monitoringApi
      .saveAutoWarmupPreference(payload)
      .then(() => {
        // A later successful save clears the error latch so a subsequent
        // failure of the SAME payload can toast again.
        if (autoWarmupErrorKeyRef.current === key) autoWarmupErrorKeyRef.current = "";
      })
      .catch((err) => {
        // Allow a retry on the next dependency change, and surface the failure
        // so the user knows their auto-warmup database selection was NOT saved
        // (previously this failed silently and the toggle looked persisted).
        autoWarmupSyncKeyRef.current = "";
        if (autoWarmupErrorKeyRef.current !== key) {
          autoWarmupErrorKeyRef.current = key;
          toast(
            `Auto-warmup preference not saved: ${formatApiError(err, "storage")}`,
            "error",
          );
        }
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
    toast,
  ]);

  const { shardMutation, shardError, shardingDb } = useClusterShardMutation({
    subscriptionId,
    storageAccount,
    storageResourceGroup,
  });

  const expansionExtras =
    showOperationalDetails || trans === "starting" ? (
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          gap: 6,
          padding: "6px 0 0 0",
        }}
      >
        {trans === "starting" && (
          <StartEstimatePanel
            clusterName={c.name}
            autoWarmupDbCount={autoWarmupDbs.size}
            startedAt={startedAt}
            progress={startProgress}
          />
        )}
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
        {showOperationalDetails && (
          <AutoStopPanel
            subscriptionId={subscriptionId}
            resourceGroup={resourceGroup}
            clusterName={c.name}
            clusterIsRunning={isRunning}
          />
        )}
      </div>
    ) : null;

  return (
    <li style={{ listStyle: "none" }}>
      <ClusterPulse
        cluster={c}
        subscriptionId={subscriptionId}
        resourceGroup={resourceGroup}
        trans={trans}
        startingStatusLine={startingLine}
        actionLoading={actionLoading}
        onStartStop={onStartStop}
        onDelete={onDelete}
        dbCounts={{
          ready: dbChips.length,
          unavailable: infeasibleDbs.length,
        }}
        expansionExtras={expansionExtras}
        onOpenDetail={() => setDetailOpen(true)}
      />

      {/* Controlled-mode details modal. The trigger UI lives inside
          <ClusterPulse>; we keep the modal mounted here so a single
          source of truth (subscriptionId/resourceGroup/cluster) feeds
          it without prop drilling. */}
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
        hideTrigger
        open={detailOpen}
        onOpenChange={setDetailOpen}
      />
    </li>
  );
}
