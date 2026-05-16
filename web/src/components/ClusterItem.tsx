import { useState } from "react";
import {
  Loader2,
  Play,
  Square,
  ChevronDown,
  Trash2,
  Flame,
  Database,
  Layers,
} from "lucide-react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";

import type { AksClusterSummary, WarmupDbInfo } from "@/api/endpoints";
import { monitoringApi, blastApi } from "@/api/endpoints";
import type { BlastDatabase } from "@/api/blast";
import { ClusterDetails } from "@/components/ClusterDetailModal";
import { useAksSkus } from "@/hooks/useAksSkus";

const CLUSTER_COLLAPSED_KEY = "elb-cluster-collapsed-";

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
  const isRunning = c.power_state === "Running";
  const [collapsed, setCollapsed] = useState(() => {
    try {
      const v = localStorage.getItem(CLUSTER_COLLAPSED_KEY + c.name);
      return v != null ? v === "1" : isStopped; // Stopped clusters collapsed by default
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

  // Warmup status — only poll when cluster is running
  const warmupQuery = useQuery({
    queryKey: ["warmup-status", subscriptionId, resourceGroup, c.name],
    queryFn: () => monitoringApi.warmupStatus(subscriptionId, resourceGroup, c.name),
    enabled: isRunning && !transitioning.has(c.name),
    staleTime: 30_000,
    refetchInterval: isRunning ? 60_000 : false,
    retry: 1,
  });
  const warmupDbs: WarmupDbInfo[] = warmupQuery.data?.databases ?? [];
  const isWarm = warmupQuery.data?.warm ?? false;

  // SKU table for the pool capacity readout (#6). React Query dedupes the
  // request so all ClusterItems share the same in-flight call.
  const { skus } = useAksSkus();
  const skuByName = new Map(skus.map((s) => [s.name, s]));

  // #4-B — active BLAST submissions for this cluster. The dashboard's own
  // `/api/blast/jobs` returns either {jobs: [...]} (real or empty list) or
  // {jobs: [], degraded: true} when the state-store table isn't configured.
  // We treat "degraded" as "tracking unavailable" and render the runtime
  // line accordingly so users can tell static capacity from "is my submit
  // done?".
  const blastJobsQuery = useQuery({
    queryKey: ["blast-jobs-for-cluster", c.name],
    queryFn: () => blastApi.listJobs(),
    enabled: isRunning && !transitioning.has(c.name),
    staleTime: 30_000,
    refetchInterval: isRunning ? 60_000 : false,
    retry: 0,
  });
  const activeSubmissionsAvailable =
    blastJobsQuery.data != null &&
    !(blastJobsQuery.data as unknown as { degraded?: boolean }).degraded;
  const activeSubmissions = (() => {
    const rows = blastJobsQuery.data?.jobs ?? [];
    const ACTIVE = new Set([
      "Provisioning",
      "DownloadingDB",
      "Splitting",
      "Running",
      "Submitted",
      "InProgress",
      "Pending",
    ]);
    return rows.filter((row) => {
      const r = row as unknown as {
        status?: string;
        phase?: string;
        infrastructure?: { cluster_name?: string };
        payload?: { cluster_name?: string };
      };
      const cluster =
        r.infrastructure?.cluster_name ?? r.payload?.cluster_name ?? null;
      if (cluster && cluster !== c.name) return false;
      const phase = r.phase ?? r.status ?? "";
      return ACTIVE.has(phase);
    });
  })();

  // BLAST databases that exist in the workload Storage account. Used to show
  // "sharded" / "downloaded" indicators alongside the live warmup status
  // (which only knows about k8s setup-jobs + db-warmup daemonsets and is
  // empty when neither was scheduled). Cluster topology is passed so the
  // backend can attach a `warmup_plan` to each DB row (Phase 1 of the
  // warmup pipeline). Cache key includes topology so this call is *not*
  // deduped with the storage-card listing (which has no plan).
  const clusterNumNodes = c.node_count ?? 0;
  const clusterMachineType = c.node_sku ?? "";
  const dbListQuery = useQuery({
    queryKey: [
      "blast-databases-with-plan",
      subscriptionId,
      storageAccount ?? "",
      storageResourceGroup ?? "",
      clusterNumNodes,
      clusterMachineType,
    ],
    queryFn: () =>
      blastApi.listDatabases(
        subscriptionId,
        storageAccount as string,
        storageResourceGroup as string,
        clusterNumNodes > 0 && clusterMachineType
          ? { numNodes: clusterNumNodes, machineType: clusterMachineType }
          : undefined,
      ),
    enabled: isRunning && !!storageAccount && !!storageResourceGroup,
    staleTime: 60_000,
    retry: 0,
    // Tighten the poll cadence while any DB is mid-shard so the chip
    // strip flips state quickly when the daemon (or the auto-shard
    // step inside warmup) finishes. Falls back to no auto-refetch
    // otherwise — the staleTime invalidate covers normal refresh.
    refetchInterval: (query) => {
      const databases = (query.state.data as { databases?: BlastDatabase[] } | undefined)
        ?.databases;
      const anyInFlight = databases?.some((d) => d.sharding_in_progress) ?? false;
      return anyInFlight ? 5_000 : false;
    },
  });
  const dbListDegraded =
    (dbListQuery.data as unknown as { degraded?: boolean })?.degraded === true;
  const databasesInStorage = dbListQuery.data?.databases ?? [];

  // Per-DB sharding mutation. Triggered by clicking a "downloaded only"
  // chip — the backend runs ensure_shard_sets() against the existing
  // download and updates the metadata blob so the next listDatabases
  // poll reports sharded=true. While the call is in flight we render
  // the chip as a transient "sharding…" state so the user gets feedback
  // without waiting for the next 60s refetch.
  const queryClient = useQueryClient();
  const [shardError, setShardError] = useState<{ name: string; msg: string } | null>(
    null,
  );
  const shardMutation = useMutation({
    mutationFn: async (dbName: string) => {
      if (!storageAccount || !storageResourceGroup) {
        throw new Error("storage account not selected");
      }
      return blastApi.shardDatabase(
        subscriptionId,
        storageResourceGroup,
        storageAccount,
        dbName,
      );
    },
    onSuccess: () => {
      setShardError(null);
      // Refetch immediately — the server-side metadata blob is now
      // marked sharding_in_progress=true, which flips the chip to the
      // "sharding…" state and turns on the 5s refetchInterval until
      // the daemon thread reports back. Invalidate BOTH the storage-card
      // listing ("blast-databases") and the cluster-card listing-with-plan
      // ("blast-databases-with-plan") so both views refresh together.
      void queryClient.invalidateQueries({
        predicate: (q) => {
          const k = q.queryKey;
          return (
            Array.isArray(k) &&
            (k[0] === "blast-databases" || k[0] === "blast-databases-with-plan") &&
            k[1] === subscriptionId &&
            k[2] === (storageAccount ?? "") &&
            k[3] === (storageResourceGroup ?? "")
          );
        },
      });
    },
    onError: (err, dbName) => {
      const msg = err instanceof Error ? err.message : String(err);
      // 409 Conflict from the per-(account, db) lock — another tab or
      // a previous click already triggered the daemon. Refetch to pull
      // the in-progress flag and clear the local error UI.
      if (msg.includes("409") || msg.toLowerCase().includes("already in progress")) {
        setShardError(null);
        void queryClient.invalidateQueries({
          predicate: (q) => {
            const k = q.queryKey;
            return (
              Array.isArray(k) &&
              (k[0] === "blast-databases" || k[0] === "blast-databases-with-plan") &&
              k[1] === subscriptionId &&
              k[2] === (storageAccount ?? "") &&
              k[3] === (storageResourceGroup ?? "")
            );
          },
        });
        return;
      }
      setShardError({ name: dbName, msg: msg.slice(0, 160) });
    },
  });
  const shardingDb = shardMutation.isPending ? shardMutation.variables : null;

  // Merge warmup-status (per-DB k8s job state) + storage listing (per-DB
  // sharded layouts) into a single ordered chip list. Every DB the platform
  // "knows about" gets one chip; badges accumulate (warmed / sharded).
  type DbChip = {
    name: string;
    warm?: WarmupDbInfo;
    sharded: boolean;
    shardLayouts: number;
    shardingInProgress: boolean;
    shardingError: string | null;
    /** Server-computed warmup feasibility — only set when cluster topology was supplied. */
    warmupPlan?: BlastDatabase["warmup_plan"];
  };
  const dbChips: DbChip[] = (() => {
    const byName = new Map<string, DbChip>();
    for (const db of databasesInStorage) {
      byName.set(db.name, {
        name: db.name,
        sharded: !!db.sharded && (db.shard_sets?.length ?? 0) > 0,
        shardLayouts: db.shard_sets?.length ?? 0,
        shardingInProgress: !!db.sharding_in_progress,
        shardingError: db.sharding_error ?? null,
        warmupPlan: db.warmup_plan,
      });
    }
    for (const w of warmupDbs) {
      const existing = byName.get(w.name);
      if (existing) existing.warm = w;
      else
        byName.set(w.name, {
          name: w.name,
          warm: w,
          sharded: false,
          shardLayouts: 0,
          shardingInProgress: false,
          shardingError: null,
        });
    }
    return Array.from(byName.values()).sort((a, b) => a.name.localeCompare(b.name));
  })();

  // Phase 1 warmup feasibility — surface a banner when at least one DB
  // would refuse warmup on the current cluster topology. The planner
  // status `ok` and `ok_unknown_sku` are silent (no banner). Anything
  // else gets called out so the user does not click "Warmup" and wait
  // for it to fail at the DaemonSet stage.
  const infeasibleDbs = dbChips.filter(
    (d) =>
      d.warmupPlan != null &&
      d.warmupPlan.feasible === false &&
      // Skip the trivial degenerate cases — they are explained elsewhere
      // ("DB still downloading", "no nodes" already shown by Pool card).
      d.warmupPlan.status !== "no_db_size" &&
      d.warmupPlan.status !== "no_nodes",
  );

  const trans = transitioning.get(c.name);
  const powerLabel =
    trans === "starting"
      ? "Starting..."
      : trans === "stopping"
        ? "Stopping..."
        : (c.power_state ?? "?");
  const powerColor =
    trans === "starting"
      ? "var(--accent)"
      : trans === "stopping"
        ? "var(--warning)"
        : c.power_state === "Running"
          ? "var(--success)"
          : "var(--warning)";

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
      {/* Header band — cluster identity, status, location, version, actions */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 10,
          padding: "10px 14px",
          cursor: "pointer",
          flexWrap: "wrap",
          borderBottom: collapsed ? "none" : "1px solid var(--border-weak)",
          background: "rgba(255, 255, 255, 0.02)",
        }}
        onClick={toggleCollapse}
      >
        <ChevronDown
          size={14}
          style={{
            transform: collapsed ? "rotate(-90deg)" : "rotate(0)",
            transition: "transform 0.15s",
            color: "var(--text-faint)",
            flexShrink: 0,
          }}
        />
        <strong
          style={{
            fontSize: 14,
            letterSpacing: "0.01em",
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
          }}
        >
          {c.name}
        </strong>
        <span
          style={{ fontSize: 11, color: powerColor, fontWeight: 600, flexShrink: 0 }}
        >
          {(trans === "starting" || trans === "stopping") && (
            <Loader2
              size={10}
              className="spin"
              style={{ verticalAlign: "middle", marginRight: 3 }}
            />
          )}
          {powerLabel}
        </span>
        {/* #7 — Workspace ready chip lives next to the power label so the
            "is this cluster usable?" signal stays in one place. */}
        {isRunning && isWarm && (
          <span
            className="dv3-warmup-chip"
            style={{ fontSize: 10, padding: "2px 7px" }}
            title={
              warmupDbs.length > 0
                ? `${warmupDbs.length} database${warmupDbs.length === 1 ? "" : "s"} warmed`
                : "Workspace ready"
            }
          >
            <Flame size={10} strokeWidth={1.75} /> ready
          </span>
        )}
        {/* Inline location + k8s version — moved up from a separate row so the
            header carries all the identity bits in a single line (#1). */}
        <span
          className="muted"
          style={{
            fontSize: 11,
            display: "inline-flex",
            gap: 8,
            flexWrap: "wrap",
            flexShrink: 1,
            minWidth: 0,
          }}
        >
          <span>· {c.region}</span>
          <span>· k8s {c.k8s_version ?? "?"}</span>
          {(c.agent_pools?.length ?? 0) === 0 && (
            <>
              <span>· {c.node_count ?? "?"} nodes</span>
              <span>({c.node_sku ?? "?"})</span>
            </>
          )}
        </span>
        {/* #11 — Stop/Delete grouped behind a vertical divider, pushed to the
            far right so destructive actions don't sit shoulder-to-shoulder
            with the cluster name. */}
        <div
          style={{
            display: "flex",
            gap: "var(--space-2)",
            alignItems: "center",
            flexShrink: 0,
            marginLeft: "auto",
            paddingLeft: 10,
            borderLeft: "1px solid var(--border-weak)",
          }}
          onClick={(e) => e.stopPropagation()}
        >
          {!trans && c.power_state === "Stopped" && (
            <button
              className="glass-button"
              onClick={() => onStartStop(c.name, "start")}
              disabled={actionLoading !== null}
              style={{ fontSize: 10, padding: "2px 8px", color: "var(--success)" }}
              title="Start cluster"
            >
              {actionLoading === `start-${c.name}` ? (
                <Loader2 size={10} className="spin" />
              ) : (
                <Play size={10} strokeWidth={1.5} />
              )}{" "}
              Start
            </button>
          )}
          {!trans && c.power_state === "Running" && (
            <button
              className="glass-button"
              onClick={() => onStartStop(c.name, "stop")}
              disabled={actionLoading !== null}
              style={{ fontSize: 10, padding: "2px 8px", color: "var(--warning)" }}
              title="Stop cluster (saves cost)"
            >
              {actionLoading === `stop-${c.name}` ? (
                <Loader2 size={10} className="spin" />
              ) : (
                <Square size={10} strokeWidth={1.5} />
              )}{" "}
              Stop
            </button>
          )}
          <button
            className="glass-button"
            onClick={() => onDelete(c.name)}
            disabled={actionLoading !== null}
            style={{ fontSize: 10, padding: "2px 8px", color: "var(--danger)" }}
            title="Delete cluster"
          >
            {actionLoading === `delete-${c.name}` ? (
              <Loader2 size={10} className="spin" />
            ) : (
              <Trash2 size={10} strokeWidth={1.5} />
            )}
          </button>
        </div>
      </div>

      {/* Body — only rendered when expanded. All children share the same
          14px horizontal padding so they line up cleanly under the header
          (no more `marginLeft: 22` hacks). */}
      {!collapsed && (
      <div style={{ padding: "12px 14px", display: "flex", flexDirection: "column", gap: 10 }}>
      {/* Per-pool cards — system / user nodepools (v3 chrome) */}
      {c.agent_pools && c.agent_pools.length > 0 && (
        <div className="dv3-pool-grid">
          {c.agent_pools.map((pool) => {
            const isSystem = (pool.mode ?? "").toLowerCase() === "system";
            const roleLabel = isSystem ? "SYSTEM" : "USER";
            const scale =
              pool.enable_auto_scaling &&
              pool.min_count != null &&
              pool.max_count != null
                ? `${pool.min_count}–${pool.max_count}`
                : `${pool.count ?? "?"}`;
            // #6 — derive cores/GiB totals from the static SKU table so each
            // pool card carries an at-a-glance capacity readout.
            const sku = skuByName.get(pool.vm_size ?? "");
            const nodes = pool.count ?? 0;
            const totalCores = sku ? sku.vCPUs * nodes : null;
            const totalGiB = sku ? sku.memoryGiB * nodes : null;
            return (
              <div
                key={pool.name}
                className={`dv3-pool-card ${isSystem ? "system" : "user"}`}
                title={`${pool.name} · mode=${pool.mode ?? "?"} · os=${
                  pool.os_type ?? "?"
                }${pool.enable_auto_scaling ? " · autoscale on" : ""}`}
              >
                {/* #7 — single primary label per card (the colored stripe on
                    .system / .user already encodes the role); the verbose
                    pool-name moves into the title tooltip to drop the
                    SYSTEM + systempool / USER + blastpool redundancy. */}
                <div className="head">
                  <span className="role">{roleLabel}</span>
                  <span
                    className="pool-name muted"
                    style={{ fontSize: 10, fontWeight: 400, opacity: 0.7 }}
                  >
                    {pool.name}
                  </span>
                </div>
                <div className="body">
                  <span className="count">{scale}</span>
                  <span>×</span>
                  <span className="sku">{pool.vm_size ?? "?"}</span>
                </div>
                <div className="footer">
                  {sku ? (
                    <>
                      {sku.vCPUs} cores · {sku.memoryGiB} GiB / node
                      {totalCores != null && totalGiB != null && nodes > 1 && (
                        <>
                          {" · "}
                          <span style={{ color: "var(--text-muted)" }}>
                            {totalCores} / {totalGiB} GiB total
                          </span>
                        </>
                      )}
                    </>
                  ) : (
                    <>{pool.enable_auto_scaling && "autoscale enabled"}</>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Sharding capacity + active-submission lines — informational, only on
          Running clusters. #4-B: rename to make the static-capacity meaning
          explicit and add a follow-up runtime line so users can tell
          "infra ceiling" apart from "is my BLAST submission done?". */}
      {isRunning && c.agent_pools && c.agent_pools.length > 0 && (() => {
        const userPool = c.agent_pools.find(
          (p) => (p.mode ?? "").toLowerCase() !== "system",
        );
        if (!userPool) return null;
        const nodes = userPool.enable_auto_scaling
          ? userPool.max_count ?? userPool.count ?? 0
          : userPool.count ?? 0;
        if (!nodes) return null;
        return (
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <div className="dv3-shard-capacity">
              <span className="lead">Sharding capacity</span>
              <code>up to {nodes} parallel jobs</code>
              <span>·</span>
              <code>{userPool.vm_size ?? "?"}</code>
              <span
                className="muted"
                title="This is the infrastructure ceiling. elastic-blast picks the actual shard count per submit; we cap each submit at 10 jobs to keep ARM throttling out of the critical path."
              >
                · max 10 jobs per submit · static capacity
              </span>
            </div>
            {/* #4-B — runtime submission state. Backend stub returns either
                {jobs: [...]} or {degraded: true}; both render gracefully. */}
            {(() => {
              const submissions = activeSubmissions;
              const tracking = activeSubmissionsAvailable;
              return (
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 6,
                    fontSize: 10,
                    color: "var(--text-muted)",
                    paddingLeft: 2,
                  }}
                >
                  <span
                    style={{
                      fontSize: 9,
                      textTransform: "uppercase",
                      letterSpacing: "0.06em",
                    }}
                  >
                    Active
                  </span>
                  {!tracking && (
                    <span
                      title="BLAST job tracking is not configured yet (api/blast/jobs returned a degraded response). Sharding-capacity above is the static ceiling."
                    >
                      · submission tracking unavailable
                    </span>
                  )}
                  {tracking && submissions.length === 0 && (
                    <span>· no active BLAST submission</span>
                  )}
                  {tracking && submissions.length > 0 && (
                    <span style={{ color: "var(--accent)" }}>
                      · {submissions.length} submission
                      {submissions.length === 1 ? "" : "s"} running
                      {submissions[0].phase
                        ? ` (${submissions[0].phase}${submissions.length > 1 ? ", …" : ""})`
                        : ""}
                    </span>
                  )}
                </div>
              );
            })()}
          </div>
        );
      })()}

      {/* Per-database readiness strip — each chip explicitly names its
          stage (downloaded / sharded / warming / ready / failed) so users
          can tell incomplete state from in-progress at a glance. The header
          `· ready` chip is only the *workspace* (cluster init) signal;
          this strip is the per-database breakdown that actually matters. */}
      {isRunning && (dbChips.length > 0 || dbListDegraded) && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <div
            style={{
              display: "flex",
              alignItems: "baseline",
              gap: 10,
              flexWrap: "wrap",
              paddingLeft: 2,
            }}
          >
            <span
              className="muted"
              style={{
                fontSize: 9,
                textTransform: "uppercase",
                letterSpacing: "0.06em",
              }}
            >
              Databases
            </span>
            {/* Inline legend so the chip colors are self-documenting. The
                pipeline is: downloaded → sharded → warming → ready. */}
            <span
              className="muted"
              style={{
                fontSize: 10,
                display: "inline-flex",
                gap: 8,
                flexWrap: "wrap",
              }}
              title="BLAST DB pipeline: download → prepare-db (sharding) → db-warmup daemonset (vmtouch) → ready. A db-warmup daemonset references sharded files, so 'warming' implies sharding is already complete."
            >
              <LegendDot color="var(--text-muted)" label="downloaded" />
              <LegendDot color="#b9a8fb" label="sharded" />
              <LegendDot color="var(--accent)" label="warming" />
              <LegendDot color="var(--success)" label="ready" />
            </span>
          </div>
          {dbListDegraded && dbChips.length === 0 ? (
            <div
              className="muted"
              style={{ fontSize: 10, paddingLeft: 2 }}
              title="BLAST databases listing is unavailable from this caller. Run scripts/dev/storage-public-access.sh on for local debugging, or rely on the Storage card from inside the Container App."
            >
              · listing unavailable (storage public access disabled)
            </div>
          ) : (
            <>
              {infeasibleDbs.length > 0 && (
                <div
                  role="alert"
                  style={{
                    fontSize: 11,
                    padding: "8px 10px",
                    marginBottom: 6,
                    borderRadius: 6,
                    background: "rgba(224, 123, 138, 0.10)",
                    border: "1px solid rgba(224, 123, 138, 0.35)",
                    color: "var(--text-primary)",
                    display: "flex",
                    flexDirection: "column",
                    gap: 4,
                  }}
                >
                  <div style={{ fontWeight: 600 }}>
                    Warmup not feasible for {infeasibleDbs.length} database
                    {infeasibleDbs.length === 1 ? "" : "s"} on this cluster
                    {clusterMachineType
                      ? ` (${clusterNumNodes} × ${clusterMachineType})`
                      : ""}
                    .
                  </div>
                  <ul style={{ margin: 0, paddingLeft: 16, lineHeight: 1.5 }}>
                    {infeasibleDbs.map((d) => (
                      <li key={d.name}>
                        <span style={{ fontWeight: 500 }}>{d.name}</span>
                        {": "}
                        <span className="muted">{d.warmupPlan!.message}</span>
                        {d.warmupPlan!.recommendations.length > 0 && (
                          <ul
                            style={{
                              margin: "2px 0 0 0",
                              paddingLeft: 14,
                              fontSize: 10,
                              color: "var(--text-muted)",
                            }}
                          >
                            {d.warmupPlan!.recommendations.map((rec, i) => (
                              <li key={i}>{rec}</li>
                            ))}
                          </ul>
                        )}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              <div className="dv3-warmup-strip" style={{ marginTop: 0 }}>
              {dbChips.map((db) => {
                const w = db.warm;
                const isReady = w?.status === "Ready";
                const isLoading = w?.status === "Loading";
                const isFailed = w?.status === "Failed";
                // Server-side in-progress flag wins over the local
                // mutation pending state — that way a page reload (or
                // a second tab) still shows "sharding…" while the
                // daemon thread is mid-run. The optimistic local
                // signal still covers the gap between POST returning
                // 202 and the next list-databases poll completing.
                const isShardingNow =
                  db.shardingInProgress || shardingDb === db.name;
                // Stage classification — mutually exclusive labels so the
                // chip's text always answers "what step is this DB at?".
                let stageLabel = "";
                let stageVariant = "";
                let StageIcon = Database;
                if (isShardingNow) {
                  stageLabel = "sharding…";
                  stageVariant = "loading";
                  StageIcon = Loader2;
                } else if (isReady) {
                  // warmup Ready implies the daemonset rolled out; sharding
                  // is a hard prereq for db-warmup so this is the goal state.
                  stageLabel = `ready · ${w!.nodes_ready}/${w!.total_jobs}`;
                  stageVariant = "";
                  StageIcon = Flame;
                } else if (isLoading) {
                  stageLabel = `warming · ${w!.nodes_ready}/${w!.total_jobs}`;
                  stageVariant = "loading";
                  StageIcon = Loader2;
                } else if (isFailed) {
                  stageLabel = `warmup failed · ${w!.nodes_failed}/${w!.total_jobs}`;
                  stageVariant = "warn";
                  StageIcon = Database;
                } else if (db.shardingError) {
                  // Sharding daemon (or warmup auto-shard) failed — show
                  // the error inline on the chip so the user can retry by
                  // re-clicking the chip without hunting for the warning
                  // line below the strip.
                  stageLabel = "shard failed · click to retry";
                  stageVariant = "warn";
                  StageIcon = Database;
                } else if (db.sharded) {
                  stageLabel = `sharded · ×${db.shardLayouts}`;
                  stageVariant = "shard";
                  StageIcon = Layers;
                } else {
                  stageLabel = "downloaded only";
                  stageVariant = "faint";
                  StageIcon = Database;
                }
                // A downloaded-only chip (or a chip whose previous shard
                // attempt errored) is the actionable state. We refuse
                // to fire while another shard mutation is in flight to
                // keep the per-cluster traffic low — the lock on the
                // server is the real guard.
                const isShardable =
                  !isShardingNow &&
                  (!db.sharded || !!db.shardingError) &&
                  !w &&
                  !shardMutation.isPending;
                const titleParts: string[] = [db.name];
                titleParts.push(stageLabel);
                if (db.shardingError) {
                  titleParts.push(db.shardingError);
                  titleParts.push("click to retry sharding");
                } else if (db.sharded && !isReady && !isLoading && !isFailed) {
                  titleParts.push(
                    `prepare-db has uploaded ${db.shardLayouts} preset layout${db.shardLayouts === 1 ? "" : "s"} — ready for elastic-blast submit (no node-side cache yet)`,
                  );
                } else if (isShardable) {
                  titleParts.push("click to run prepare-db sharding now");
                } else if (!db.sharded && !w) {
                  titleParts.push(
                    "prepare-db has not uploaded shard layouts yet — elastic-blast will shard on first submit",
                  );
                } else if (isReady) {
                  titleParts.push("vmtouch cache hot on every node — zero cold-start");
                }
                // Phase 1 warmup planner — show feasibility verdict in
                // the chip tooltip whenever the server returned a plan.
                // This is silent on `ok` to avoid noise; warning verdicts
                // get the message + every recommendation appended.
                if (db.warmupPlan && db.warmupPlan.status !== "ok") {
                  titleParts.push(`warmup: ${db.warmupPlan.message}`);
                  for (const rec of db.warmupPlan.recommendations) {
                    titleParts.push(`→ ${rec}`);
                  }
                }
                const baseClass = `dv3-warmup-chip${stageVariant ? " " + stageVariant : ""}`;
                const chipBody = (
                  <>
                    <StageIcon
                      size={11}
                      strokeWidth={1.75}
                      className={isLoading || isShardingNow ? "spin" : undefined}
                    />
                    {db.name}
                    <span className="stage">{stageLabel}</span>
                  </>
                );
                return isShardable ? (
                  <button
                    key={db.name}
                    type="button"
                    className={baseClass}
                    title={titleParts.join(" · ")}
                    onClick={() => shardMutation.mutate(db.name)}
                    style={{
                      cursor: "pointer",
                      font: "inherit",
                      appearance: "none",
                      // Slight lift on hover via filter so we don't need a
                      // dedicated CSS rule per variant.
                      filter: "brightness(1)",
                    }}
                    onMouseEnter={(e) => {
                      e.currentTarget.style.filter = "brightness(1.18)";
                    }}
                    onMouseLeave={(e) => {
                      e.currentTarget.style.filter = "brightness(1)";
                    }}
                  >
                    {chipBody}
                  </button>
                ) : (
                  <span
                    key={db.name}
                    className={baseClass}
                    title={titleParts.join(" · ")}
                  >
                    {chipBody}
                  </span>
                );
              })}
            </div>
            </>
          )}
          {shardError && (
            <div
              className="muted"
              style={{ fontSize: 10, paddingLeft: 2, color: "var(--warning)" }}
              title={shardError.msg}
            >
              · sharding failed for {shardError.name}: {shardError.msg}
            </div>
          )}
        </div>
      )}

      {/* STATE row only renders when ARM provisioning is NOT a steady
          "Succeeded". The top-of-card "Running" chip already conveys the
          healthy case; surfacing a redundant green pill just adds noise. */}
      {(() => {
        const ps = c.provisioning_state ?? "?";
        if (ps === "Succeeded" || ps === "?") return null;
            return (
              <div
                style={{
                  fontSize: 11,
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                }}
              >
                <span
                  className="muted"
                  style={{
                    fontSize: 9,
                    textTransform: "uppercase",
                    letterSpacing: "0.06em",
                  }}
                >
                  State
                </span>
                {(ps === "Creating" || ps === "Updating") && (
                  <span
                    className="dv3-pill dv3-pill-accent"
                    style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
                  >
                    <Loader2 size={10} className="spin" />
                    {ps}
                  </span>
                )}
                {ps === "Deleting" && (
                  <span
                    className="dv3-pill dv3-pill-warning"
                    style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
                  >
                    <Loader2 size={10} className="spin" />
                    {ps}
                  </span>
                )}
                {ps === "Failed" && (
                  <span className="dv3-pill dv3-pill-danger">{ps}</span>
                )}
                {ps !== "Creating" &&
                  ps !== "Updating" &&
                  ps !== "Deleting" &&
                  ps !== "Failed" && (
                    <span className="dv3-pill dv3-pill-faint">{ps}</span>
                  )}
              </div>
            );
          })()}
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
      </div>
      )}
    </li>
  );
}

// Small colored-dot + label used by the Databases legend strip.
function LegendDot({ color, label }: { color: string; label: string }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 3 }}>
      <span
        style={{
          width: 6,
          height: 6,
          borderRadius: "50%",
          background: color,
          flexShrink: 0,
        }}
      />
      {label}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Cluster Details — compact inline summary + modal for full details
// ---------------------------------------------------------------------------
