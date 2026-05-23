/**
 * WarmupSection — DB cache warmup panel shown inside the AKS cluster detail modal.
 *
 * Shows which databases are already warm on the cluster nodes, and lets the user
 * start a standalone warmup for downloaded databases. Uses the warmup/start
 * orchestrator endpoint.
 */
import { useState, useEffect, useMemo } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Database,
  Flame,
  Loader2,
  RefreshCw,
  Snowflake,
  Trash2,
} from "lucide-react";
import {
  type UseQueryResult,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import { monitoringApi, blastApi } from "@/api/endpoints";
import type { K8sNodeMetrics, WarmupDbInfo, WarmupStatus } from "@/api/endpoints";
import { formatApiError } from "@/api/client";
import type { ApiError } from "@/api/client";
import { useToast } from "@/components/Toast";
import {
  WARMUP_CANDIDATES,
  type WarmupCapacity,
  type WarmupRow,
  buildWarmupRows,
  formatDuration,
  shortWarmupPhase,
  summariseWarmupCapacity,
} from "@/components/warmupSection/helpers";

interface Props {
  subscriptionId: string;
  resourceGroup: string;
  clusterName: string;
  warmupDbs?: WarmupDbInfo[];
  warmupQuery?: UseQueryResult<WarmupStatus>;
  storageAccount?: string;
  storageResourceGroup?: string;
  acrResourceGroup?: string;
  acrName?: string;
  region?: string;
  nodeSku?: string | null;
  nodeCount?: number | null;
  nodeMetrics?: K8sNodeMetrics[];
  terminalResourceGroup?: string;
  terminalVmName?: string;
}

export function WarmupSection({
  subscriptionId,
  resourceGroup,
  clusterName,
  warmupDbs = [],
  warmupQuery,
  storageAccount,
  storageResourceGroup,
  acrResourceGroup,
  acrName,
  region,
  nodeSku,
  nodeCount,
  nodeMetrics = [],
  terminalResourceGroup,
  terminalVmName,
}: Props) {
  const [selectedDb, setSelectedDb] = useState("");
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [warmupInstanceId, setWarmupInstanceId] = useState<string | null>(() => {
    try {
      const stored = localStorage.getItem(`elb-warmup-${clusterName}`);
      return stored || null;
    } catch {
      return null;
    }
  });
  const [startError, setStartError] = useState<string | null>(null);
  const [releaseNotice, setReleaseNotice] = useState<{
    db: string;
    status: "pending" | "success" | "partial" | "error";
    message: string;
  } | null>(null);
  const [starting, setStarting] = useState(false);

  // Query downloaded databases from storage (to know which are available for warmup).
  // Passing cluster topology asks the backend to attach a `warmup_plan` to each
  // DB row — Phase 1 of the warmup pipeline. Cache key includes topology so
  // this call is *not* deduped with the storage-card listing (which has no
  // plan), matching the cluster-card listing's cache key shape.
  const downloadedQuery = useQuery({
    queryKey: [
      "blast-databases-warmup",
      subscriptionId,
      storageAccount,
      storageResourceGroup,
      nodeCount ?? 0,
      nodeSku ?? "",
    ],
    queryFn: () =>
      blastApi.listDatabases(
        subscriptionId,
        storageAccount!,
        storageResourceGroup || resourceGroup,
        nodeCount && nodeCount > 0 && nodeSku
          ? { numNodes: nodeCount, machineType: nodeSku }
          : undefined,
      ),
    enabled: Boolean(subscriptionId && storageAccount),
    staleTime: 120_000,
  });
  // Index plans by db name for O(1) lookup in the select / button.
  const planByName = useMemo(
    () =>
      new Map(
        (downloadedQuery.data?.databases ?? [])
          .filter((d) => d.warmup_plan != null)
          .map((d) => [d.name, d.warmup_plan!] as const),
      ),
    [downloadedQuery.data?.databases],
  );
  const selectedPlan = selectedDb ? planByName.get(selectedDb) : undefined;
  // Block Warmup if the planner says it cannot fit. Degenerate `no_db_size` is
  // not actionable here (size missing → planner can't decide), so we let it
  // through and rely on the orchestrator's own checks.
  const selectedInfeasible =
    selectedPlan != null &&
    selectedPlan.feasible === false &&
    selectedPlan.status !== "no_db_size";

  const capacity = useMemo(() => summariseWarmupCapacity(nodeMetrics), [nodeMetrics]);
  const warmupRows = useMemo(
    () =>
      buildWarmupRows({
        databases: downloadedQuery.data?.databases ?? [],
        warmupDbs,
        planByName,
        capacity,
      }),
    [downloadedQuery.data?.databases, warmupDbs, planByName, capacity],
  );

  const releaseMutation = useMutation({
    mutationFn: (dbName: string) =>
      monitoringApi.releaseWarmup({
        subscription_id: subscriptionId,
        resource_group: resourceGroup,
        aks_cluster_name: clusterName,
        db: `blast-db/${dbName}`,
      }),
    onMutate: (dbName) => {
      setReleaseNotice({
        db: dbName,
        status: "pending",
        message: `Releasing warm cache for ${dbName}...`,
      });
    },
    onSuccess: async (result, dbName) => {
      await Promise.all([
        warmupQuery?.refetch(),
        downloadedQuery.refetch(),
        queryClient.invalidateQueries({
          queryKey: ["warmup-status", subscriptionId, resourceGroup, clusterName],
        }),
        queryClient.invalidateQueries({
          queryKey: ["warmup-status-submit", subscriptionId, resourceGroup, clusterName],
        }),
      ]);
      const deleted = result.deleted?.length ?? 0;
      const errors = result.errors?.length ?? 0;
      const isPartial = result.status === "partial" || errors > 0;
      const message = isPartial
        ? `Warm cache release for ${dbName} partially completed: ${deleted} resource${deleted === 1 ? "" : "s"} deleted, ${errors} error${errors === 1 ? "" : "s"}.`
        : `Warm cache released for ${dbName}. ${deleted} resource${deleted === 1 ? "" : "s"} deleted.`;
      setReleaseNotice({
        db: dbName,
        status: isPartial ? "partial" : "success",
        message,
      });
      toast(message, isPartial ? "warning" : "success");
    },
    onError: (err, dbName) => {
      const message = `Warm cache release failed for ${dbName}: ${formatApiError(err, "warmup")}`;
      setReleaseNotice({ db: dbName, status: "error", message });
      toast(message, "error");
    },
  });

  // Poll warmup orchestrator if one is active. 10s cadence is enough to keep
  // the timeline animated while halving the per-user request rate for a
  // long-running (minutes-to-hours) warmup operation.
  const orchQuery = useQuery({
    queryKey: ["warmup-orch", warmupInstanceId],
    queryFn: () => monitoringApi.warmupOrchStatus(warmupInstanceId!),
    enabled: Boolean(warmupInstanceId),
    refetchInterval: 10_000,
    retry: 1, // fail fast on stale instance_id
  });

  // Clear stale instance_id only when the backend explicitly says the
  // orchestrator no longer exists. Transient network or 5xx failures should not
  // erase the handle for an active warmup run.
  useEffect(() => {
    const status = (orchQuery.error as Partial<ApiError> | null)?.status;
    if (warmupInstanceId && orchQuery.isError && status === 404) {
      setWarmupInstanceId(null);
      try {
        localStorage.removeItem(`elb-warmup-${clusterName}`);
      } catch {
        /* */
      }
    }
  }, [orchQuery.error, orchQuery.isError, warmupInstanceId, clusterName]);

  // Clear instance ID when orchestrator finishes
  useEffect(() => {
    if (!orchQuery.data) return;
    const rs = orchQuery.data.runtime_status;
    if (rs === "Completed" || rs === "Failed" || rs === "Terminated") {
      // Keep showing for a bit, then clear
      const t = setTimeout(() => {
        setWarmupInstanceId(null);
        try {
          localStorage.removeItem(`elb-warmup-${clusterName}`);
        } catch {
          /* */
        }
        warmupQuery?.refetch();
      }, 10_000);
      return () => clearTimeout(t);
    }
  }, [orchQuery.data, clusterName, warmupQuery]);

  const handleStartWarmup = async (dbName = selectedDb) => {
    if (!dbName || !storageAccount || warmupInstanceId) return;
    // Defence in depth — the button is disabled when selectedInfeasible is
    // true, but keyboard activation / future programmatic calls should also
    // refuse. Showing the planner verdict in startError gives the user the
    // same fix recommendations they would see in the inline advisory.
    const plan = planByName.get(dbName);
    const infeasible =
      plan != null && plan.feasible === false && plan.status !== "no_db_size";
    if (infeasible && plan) {
      setStartError(`Warmup blocked by feasibility planner: ${plan.message}`);
      return;
    }
    setStartError(null);
    setSelectedDb(dbName);
    setStarting(true);
    try {
      const candidate = WARMUP_CANDIDATES.find((c) => c.value === dbName);
      const resp = await monitoringApi.startWarmup({
        subscription_id: subscriptionId,
        resource_group: resourceGroup,
        storage_account: storageAccount,
        storage_resource_group: storageResourceGroup || resourceGroup,
        region: region || "koreacentral",
        db: `blast-db/${dbName}`,
        db_display_name: dbName,
        program: candidate?.program || "blastn",
        aks_cluster_name: clusterName,
        machine_type: nodeSku || undefined,
        num_nodes: nodeCount || undefined,
        acr_resource_group: acrResourceGroup,
        acr_name: acrName,
        terminal_resource_group: terminalResourceGroup,
        terminal_vm_name: terminalVmName,
      });
      setWarmupInstanceId(resp.instance_id);
      try {
        localStorage.setItem(`elb-warmup-${clusterName}`, resp.instance_id);
      } catch {
        /* */
      }
    } catch (e) {
      setStartError(formatApiError(e, "warmup"));
    } finally {
      setStarting(false);
    }
  };

  const orchPhase = orchQuery.data?.custom_status?.phase;
  const orchDb = orchQuery.data?.custom_status?.db;
  const orchFinished =
    orchQuery.data?.runtime_status === "Completed" ||
    orchQuery.data?.runtime_status === "Failed";
  const orchSuccess =
    orchQuery.data?.runtime_status === "Completed" &&
    orchQuery.data?.output?.status === "succeeded";

  return (
    <div style={{ marginTop: "var(--space-3)" }}>
      <h4
        style={{
          margin: "0 0 var(--space-2) 0",
          fontSize: 13,
          fontWeight: 600,
          display: "flex",
          alignItems: "center",
          gap: 6,
        }}
      >
        <Flame size={14} strokeWidth={1.5} /> DB Warmup
        {warmupQuery?.isFetching && (
          <Loader2 size={10} className="spin" style={{ color: "var(--text-faint)" }} />
        )}
        <button
          className="glass-button"
          onClick={() => warmupQuery?.refetch()}
          style={{ padding: "2px 6px", border: "none", marginLeft: "auto" }}
          title="Refresh warmup status"
        >
          <RefreshCw size={12} strokeWidth={1.5} />
        </button>
      </h4>

      <WarmupCapacityBanner capacity={capacity} nodeSku={nodeSku} nodeCount={nodeCount} />

      <div
        style={{
          display: "flex",
          flexDirection: "column",
          gap: 8,
          marginBottom: "var(--space-2)",
        }}
      >
        {downloadedQuery.isLoading ? (
          <WarmupRowsSkeleton />
        ) : warmupRows.length === 0 ? (
          <div className="muted" style={{ fontSize: 11 }}>
            No downloaded databases are available for node cache warmup yet.
          </div>
        ) : (
          warmupRows.map((row) => (
            <WarmupDbRow
              key={row.name}
              row={row}
              actionsDisabled={Boolean(warmupInstanceId) || !storageAccount}
              starting={starting && selectedDb === row.name}
              releasing={
                releaseMutation.isPending && releaseMutation.variables === row.name
              }
              onWarm={() => {
                setSelectedDb(row.name);
                void handleStartWarmup(row.name);
              }}
              onRelease={() => releaseMutation.mutate(row.name)}
            />
          ))
        )}
      </div>

      {releaseNotice && (
        <WarmupNotice
          status={releaseNotice.status}
          message={releaseNotice.message}
          onDismiss={
            releaseNotice.status === "pending" ? undefined : () => setReleaseNotice(null)
          }
        />
      )}

      {/* Active warmup orchestrator status */}
      {warmupInstanceId && orchQuery.data && (
        <div
          style={{
            padding: "8px 12px",
            borderRadius: 8,
            marginBottom: "var(--space-2)",
            fontSize: 11,
            background: orchFinished
              ? orchSuccess
                ? "rgba(106,214,163,0.08)"
                : "rgba(224,123,138,0.08)"
              : "rgba(122,167,255,0.08)",
            border: `1px solid ${
              orchFinished
                ? orchSuccess
                  ? "rgba(106,214,163,0.2)"
                  : "rgba(224,123,138,0.2)"
                : "rgba(122,167,255,0.2)"
            }`,
            color: orchFinished
              ? orchSuccess
                ? "var(--success)"
                : "var(--danger)"
              : "var(--accent)",
            display: "flex",
            alignItems: "center",
            gap: 8,
          }}
        >
          {!orchFinished && <Loader2 size={12} className="spin" />}
          {orchFinished && orchSuccess && <CheckCircle2 size={12} strokeWidth={1.5} />}
          {orchFinished && !orchSuccess && <AlertTriangle size={12} strokeWidth={1.5} />}
          <div>
            <strong>Warmup {orchDb ? `(${orchDb})` : ""}</strong>:{" "}
            {orchPhase === "enabling_storage"
              ? "Enabling storage access..."
              : orchPhase === "configuring"
                ? "Preparing..."
                : orchPhase === "warming_up"
                  ? `Loading DB to nodes... (${(orchQuery.data?.custom_status?.steps?.warming_up as Record<string, number> | undefined)?.ready ?? 0}/${(orchQuery.data?.custom_status?.steps?.warming_up as Record<string, number> | undefined)?.total ?? "?"})`
                  : orchPhase === "completed"
                    ? "Completed"
                    : orchPhase === "failed"
                      ? `Failed: ${orchQuery.data?.output?.error?.slice(0, 100) ?? "unknown"}`
                      : (orchPhase ?? orchQuery.data.runtime_status)}
          </div>
        </div>
      )}

      {/* Phase 1 — surface the planner verdict for the currently selected
          DB. Hidden when the plan is missing (no cluster topology yet) or
          when the plan is silent (`ok`). For `ok_unknown_sku` we render an
          amber notice; otherwise red with recommendations. */}
      {selectedPlan && selectedPlan.status !== "ok" && (
        <div
          role={selectedInfeasible ? "alert" : "note"}
          style={{
            marginTop: 8,
            padding: "8px 10px",
            borderRadius: 6,
            fontSize: 11,
            background: selectedInfeasible
              ? "rgba(224, 123, 138, 0.10)"
              : "rgba(240, 198, 116, 0.10)",
            border: `1px solid ${
              selectedInfeasible
                ? "rgba(224, 123, 138, 0.35)"
                : "rgba(240, 198, 116, 0.35)"
            }`,
            color: "var(--text-primary)",
            display: "flex",
            flexDirection: "column",
            gap: 4,
          }}
        >
          <div style={{ fontWeight: 600 }}>
            {selectedInfeasible ? "Warmup blocked" : "Warmup advisory"}
          </div>
          <div className="muted">{selectedPlan.message}</div>
          {selectedPlan.recommendations.length > 0 && (
            <ul
              style={{
                margin: "2px 0 0 0",
                paddingLeft: 16,
                lineHeight: 1.5,
                color: "var(--text-muted)",
              }}
            >
              {selectedPlan.recommendations.map((rec, i) => (
                <li key={i}>{rec}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      {startError && (
        <div
          style={{
            marginTop: 6,
            fontSize: 11,
            color: "var(--danger)",
            padding: "4px 8px",
            borderRadius: 4,
            background: "rgba(224,123,138,0.08)",
          }}
        >
          {startError}
        </div>
      )}

      {!storageAccount && (
        <div style={{ fontSize: 11, color: "var(--text-faint)" }}>
          Configure a storage account in Settings to enable warmup.
        </div>
      )}
    </div>
  );
}

function WarmupCapacityBanner({
  capacity,
  nodeSku,
  nodeCount,
}: {
  capacity: WarmupCapacity;
  nodeSku?: string | null;
  nodeCount?: number | null;
}) {
  const pressure = capacity.memoryPressure;
  return (
    <div
      style={{
        marginBottom: 10,
        padding: "8px 10px",
        borderRadius: 8,
        fontSize: 11,
        background: pressure ? "rgba(240,198,116,0.10)" : "rgba(106,214,163,0.08)",
        border: `1px solid ${pressure ? "rgba(240,198,116,0.30)" : "rgba(106,214,163,0.22)"}`,
        color: "var(--text-muted)",
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 10,
        flexWrap: "wrap",
      }}
    >
      <span>
        Warm cache capacity · {nodeCount ?? capacity.nodes} node
        {(nodeCount ?? capacity.nodes) === 1 ? "" : "s"}
        {nodeSku ? ` · ${nodeSku}` : ""}
      </span>
      <span
        style={{ color: pressure ? "var(--warning)" : "var(--success)", fontWeight: 600 }}
      >
        {capacity.memoryPct == null
          ? "memory unknown"
          : `${capacity.memoryPct.toFixed(1)}% memory used`}
        {capacity.minFreeGiB != null
          ? ` · min ${capacity.minFreeGiB.toFixed(1)} GiB free`
          : ""}
        {capacity.pressureFlags.length > 0
          ? ` · ${capacity.pressureFlags.join(", ")}`
          : ""}
      </span>
    </div>
  );
}

function WarmupDbRow({
  row,
  actionsDisabled,
  starting,
  releasing,
  onWarm,
  onRelease,
}: {
  row: WarmupRow;
  actionsDisabled: boolean;
  starting: boolean;
  releasing: boolean;
  onWarm: () => void;
  onRelease: () => void;
}) {
  const tone = row.cacheTone;
  const toneColor =
    tone === "ready"
      ? "var(--success)"
      : tone === "loading"
        ? "var(--accent)"
        : tone === "blocked"
          ? "var(--danger)"
          : tone === "pressure"
            ? "var(--warning)"
            : "var(--text-muted)";
  const showWarm = row.primaryAction === "warm" || row.primaryAction === "rewarm";
  const warmDisabled = actionsDisabled || !row.canWarm || starting || releasing;
  const releaseDisabled = actionsDisabled || !row.canRelease || starting || releasing;
  return (
    <div
      className="warmup-db-card"
      style={{
        display: "grid",
        gridTemplateColumns: "minmax(160px, 1.1fr) minmax(220px, 1.5fr) auto",
        gap: 10,
        alignItems: "center",
        padding: "10px 12px",
        borderRadius: 8,
        background: "rgba(255,255,255,0.035)",
        border: "1px solid var(--border-weak)",
      }}
    >
      <div style={{ minWidth: 0 }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            fontSize: 12,
            fontWeight: 600,
          }}
        >
          <Database size={13} color="var(--accent)" />
          <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{row.name}</span>
        </div>
        <div className="muted" style={{ fontSize: 10, marginTop: 3 }}>
          {row.label} · {row.sizeLabel}
        </div>
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 4, minWidth: 0 }}>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", fontSize: 10 }}>
          <StatusPill
            label={row.storageLabel}
            tone={row.storageLabel === "Downloaded" ? "ok" : "neutral"}
          />
          <StatusPill
            label={row.shardLabel}
            tone={row.shardLabel === "Not sharded" ? "neutral" : "accent"}
          />
          <StatusPill label={row.cacheLabel} toneColor={toneColor} />
        </div>
        <div
          className="muted"
          style={{ fontSize: 10, lineHeight: 1.4 }}
          title={row.blockedReason ?? row.detail}
        >
          {row.blockedReason ?? row.detail}
        </div>
        {row.warm?.status === "Loading" && <WarmupProgressBar warm={row.warm} />}
      </div>
      <div
        className="warmup-db-card__actions"
        style={{ display: "flex", gap: 6, justifyContent: "flex-end", flexWrap: "wrap" }}
      >
        {showWarm && (
          <button
            type="button"
            className="btn btn--primary btn--sm"
            disabled={warmDisabled}
            onClick={onWarm}
            title={row.blockedReason}
            style={{ fontSize: 11, whiteSpace: "nowrap" }}
          >
            {starting ? <Loader2 size={11} className="spin" /> : <Flame size={11} />}
            {row.primaryAction === "rewarm" ? "Rewarm" : "Warm"}
          </button>
        )}
        {row.canRelease && (
          <button
            type="button"
            className="glass-button"
            disabled={releaseDisabled}
            onClick={onRelease}
            title="Release warm cache resources for this database"
            style={{ fontSize: 11, whiteSpace: "nowrap", padding: "5px 9px" }}
          >
            {releasing ? <Loader2 size={11} className="spin" /> : <Trash2 size={11} />}
            Release
          </button>
        )}
        {!showWarm && !row.canRelease && (
          <span
            className="muted"
            style={{ fontSize: 10, display: "inline-flex", alignItems: "center", gap: 4 }}
          >
            <Snowflake size={11} /> Submit cold
          </span>
        )}
      </div>
    </div>
  );
}

function WarmupRowsSkeleton() {
  return (
    <div aria-label="Loading database cache candidates" style={{ display: "grid", gap: 8 }}>
      {[0, 1, 2].map((idx) => (
        <div
          key={idx}
          className="warmup-row-skeleton"
          style={{
            display: "grid",
            gridTemplateColumns: "minmax(160px, 1.1fr) minmax(220px, 1.5fr) 88px",
            gap: 10,
            alignItems: "center",
            padding: "10px 12px",
            borderRadius: 8,
            background: "rgba(255,255,255,0.035)",
            border: "1px solid var(--border-weak)",
          }}
        >
          <SkeletonBlock width="72%" />
          <div style={{ display: "grid", gap: 6 }}>
            <SkeletonBlock width="88%" />
            <SkeletonBlock width="52%" />
          </div>
          <SkeletonBlock width="76px" />
        </div>
      ))}
    </div>
  );
}

function SkeletonBlock({ width }: { width: string }) {
  return (
    <span
      aria-hidden="true"
      className="skeleton"
      style={{
        width,
        height: 10,
        borderRadius: 999,
        display: "block",
      }}
    />
  );
}

function WarmupNotice({
  status,
  message,
  onDismiss,
}: {
  status: "pending" | "success" | "partial" | "error";
  message: string;
  onDismiss?: () => void;
}) {
  const isPending = status === "pending";
  const tone =
    status === "success"
      ? "var(--success)"
      : status === "error"
        ? "var(--danger)"
        : status === "partial"
          ? "var(--warning)"
          : "var(--accent)";
  return (
    <div
      role={status === "error" ? "alert" : "status"}
      style={{
        marginTop: 6,
        marginBottom: "var(--space-2)",
        padding: "8px 10px",
        borderRadius: 8,
        fontSize: 11,
        background: "rgba(255,255,255,0.04)",
        border: `1px solid color-mix(in srgb, ${tone} 35%, transparent)`,
        color: "var(--text-primary)",
        display: "flex",
        alignItems: "center",
        gap: 8,
      }}
    >
      {isPending ? (
        <Loader2 size={12} className="spin" style={{ color: tone }} />
      ) : status === "success" ? (
        <CheckCircle2 size={12} strokeWidth={1.5} style={{ color: tone }} />
      ) : (
        <AlertTriangle size={12} strokeWidth={1.5} style={{ color: tone }} />
      )}
      <span style={{ flex: 1 }}>{message}</span>
      {onDismiss && (
        <button
          type="button"
          className="glass-button"
          onClick={onDismiss}
          style={{ padding: "2px 7px", fontSize: 10 }}
        >
          Dismiss
        </button>
      )}
    </div>
  );
}

function WarmupProgressBar({ warm }: { warm: WarmupDbInfo }) {
  const pct = Number.isFinite(warm.progress_pct)
    ? Math.max(0, Math.min(100, warm.progress_pct ?? 0))
    : 0;
  const phase = shortWarmupPhase(warm);
  const lastLogs = (warm.pod_statuses ?? [])
    .map((pod) => pod.last_log || pod.message)
    .filter(Boolean)
    .slice(0, 3)
    .join(" · ");
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
      <div
        style={{
          height: 4,
          background: "rgba(255,255,255,0.08)",
          borderRadius: 999,
          overflow: "hidden",
        }}
        title={lastLogs || undefined}
      >
        <div
          style={{
            width: `${pct}%`,
            minWidth: pct > 0 ? 8 : 0,
            height: "100%",
            background: "var(--accent)",
            transition: "width 0.3s ease",
          }}
        />
      </div>
      <div
        style={{
          fontSize: 9,
          color: "var(--text-faint)",
          display: "flex",
          gap: 6,
          flexWrap: "wrap",
        }}
      >
        <span>{phase}</span>
        <span>
          {warm.nodes_ready}/{warm.total_jobs} ready
        </span>
        {warm.elapsed_seconds != null && (
          <span>{formatDuration(warm.elapsed_seconds)} elapsed</span>
        )}
        {warm.estimated_remaining_seconds != null ? (
          <span>~{formatDuration(warm.estimated_remaining_seconds)} left</span>
        ) : (
          <span>ETA after first shard completes</span>
        )}
      </div>
    </div>
  );
}

function StatusPill({
  label,
  tone,
  toneColor,
}: {
  label: string;
  tone?: "ok" | "accent" | "neutral";
  toneColor?: string;
}) {
  const color =
    toneColor ??
    (tone === "ok"
      ? "var(--success)"
      : tone === "accent"
        ? "var(--accent)"
        : "var(--text-muted)");
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        minHeight: 20,
        padding: "2px 7px",
        borderRadius: 999,
        border: "1px solid rgba(255,255,255,0.12)",
        color,
        background: "rgba(255,255,255,0.035)",
        whiteSpace: "nowrap",
      }}
    >
      {label}
    </span>
  );
}

