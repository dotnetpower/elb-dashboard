import { useState, useEffect, useCallback } from "react";
import { createPortal } from "react-dom";
import { useQuery } from "@tanstack/react-query";
import { Loader2, Plus, Trash2, CheckCircle2, AlertTriangle, Play, Square, Copy, ChevronDown, Terminal, Maximize2, X, RefreshCw } from "lucide-react";

import { monitoringApi, aksApi } from "@/api/endpoints";
import { MonitorCard } from "@/components/MonitorCard";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { useRefreshCountdown } from "@/hooks/useRefreshCountdown";

const DEFAULT_SKU = "Standard_E32s_v5";
const DEFAULT_NODE_COUNT = 10;

// #13: Human-readable SKU descriptions with approximate hourly cost
const SKU_INFO: Record<string, { desc: string; costPerNode: number }> = {
  "Standard_E16s_v5": { desc: "16 cores, 128 GB RAM — small databases", costPerNode: 0.67 },
  "Standard_E20s_v5": { desc: "20 cores, 160 GB RAM — medium databases", costPerNode: 0.84 },
  "Standard_E32s_v5": { desc: "32 cores, 256 GB RAM — large databases (recommended)", costPerNode: 1.34 },
  "Standard_E48s_v5": { desc: "48 cores, 384 GB RAM — very large databases", costPerNode: 2.02 },
  "Standard_E64s_v5": { desc: "64 cores, 512 GB RAM — maximum performance", costPerNode: 2.69 },
};

const CLUSTER_NAME_RE = /^[a-zA-Z][a-zA-Z0-9-]{1,62}$/;

interface Props {
  subscriptionId: string;
  resourceGroup: string;
  region?: string;
  acrResourceGroup?: string;
  acrName?: string;
  storageResourceGroup?: string;
  storageAccount?: string;
}

export function ClusterCard({
  subscriptionId,
  resourceGroup,
  region,
  acrResourceGroup,
  acrName,
  storageResourceGroup,
  storageAccount,
}: Props) {
  const enabled = Boolean(subscriptionId && resourceGroup);
  const query = useQuery({
    queryKey: ["aks", subscriptionId, resourceGroup],
    queryFn: () => monitoringApi.aks(subscriptionId, resourceGroup),
    enabled,
    refetchInterval: 30_000,
  });

  const noClusters = query.data?.clusters.length === 0;

  // Provision form state
  const [showProvision, setShowProvision] = useState(false);
  const [clusterName, setClusterName] = useState("elb-cluster");
  const [nodeSku, setNodeSku] = useState(DEFAULT_SKU);
  const [nodeCount, setNodeCount] = useState(DEFAULT_NODE_COUNT);
  const [provStatus, setProvStatus] = useState<"idle" | "creating" | "done" | "error">("idle");
  const [provError, setProvError] = useState<string | null>(null);
  const [provStart, setProvStart] = useState<number | null>(null);
  const [elapsed, setElapsed] = useState(0);

  // Role assignment result (from auto-assign during provision)
  const [roleResult, setRoleResult] = useState<string[] | null>(null);

  // Start/Stop/Delete loading
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  // Track clusters in transition (starting/stopping) until actual state changes
  const [transitioning, setTransitioning] = useState<Map<string, "starting" | "stopping">>(new Map());

  // Available SKUs
  const skuQuery = useQuery({
    queryKey: ["aks-skus"],
    queryFn: () => aksApi.listSkus(),
    enabled,
    staleTime: 600_000,
  });

  useEffect(() => {
    if (provStatus !== "creating") return;
    const timer = setInterval(() => setElapsed(Math.floor((Date.now() - (provStart ?? Date.now())) / 1000)), 1000);
    return () => clearInterval(timer);
  }, [provStatus, provStart]);

  const handleProvision = async () => {
    if (!region) return;
    setProvStatus("creating");
    setProvError(null);
    setProvStart(Date.now());
    try {
      const resp = await aksApi.provision({
        subscription_id: subscriptionId,
        resource_group: resourceGroup,
        region,
        cluster_name: clusterName,
        node_sku: nodeSku,
        node_count: nodeCount,
        acr_resource_group: acrResourceGroup || "",
        acr_name: acrName || "",
        storage_resource_group: storageResourceGroup || resourceGroup,
        storage_account: storageAccount || "",
      });
      setRoleResult(resp.roles_assigned || []);
      setProvStatus("done");
      setShowProvision(false);
      query.refetch();
    } catch (e) {
      setProvError((e as Error).message);
      setProvStatus("error");
    }
  };

  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);

  const handleDelete = async (name: string) => {
    setActionError(null);
    setActionLoading(`delete-${name}`);
    try {
      await aksApi.delete(subscriptionId, resourceGroup, name);
      query.refetch();
    } catch (e) {
      setActionError(`Delete failed: ${(e as Error).message}`);
    } finally {
      setDeleteTarget(null);
      setActionLoading(null);
    }
  };

  const handleStartStop = async (name: string, action: "start" | "stop") => {
    setActionError(null);
    setActionLoading(`${action}-${name}`);
    try {
      if (action === "start") {
        await aksApi.start(subscriptionId, resourceGroup, name);
      } else {
        await aksApi.stop(subscriptionId, resourceGroup, name);
      }
      // Mark cluster as transitioning
      setTransitioning((prev) => new Map(prev).set(name, action === "start" ? "starting" : "stopping"));
    } catch (e) {
      setActionError(`${action} failed: ${(e as Error).message}`);
    } finally {
      setActionLoading(null);
    }
  };

  // Clear transition state when actual power_state reaches target
  useEffect(() => {
    if (transitioning.size === 0 || !query.data?.clusters) return;
    const next = new Map(transitioning);
    let changed = false;
    for (const [name, expected] of transitioning) {
      const cluster = query.data.clusters.find((c) => c.name === name);
      if (!cluster) { next.delete(name); changed = true; continue; }
      const reached = expected === "starting" ? cluster.power_state === "Running" : cluster.power_state === "Stopped";
      if (reached) { next.delete(name); changed = true; }
    }
    if (changed) setTransitioning(next);
  }, [query.data, transitioning]);

  // Poll faster (10s) while clusters are transitioning
  const isTransitioning = transitioning.size > 0;
  useEffect(() => {
    if (!isTransitioning) return;
    const t = setInterval(() => query.refetch(), 10_000);
    return () => clearInterval(t);
  }, [isTransitioning, query]);

  // Auto-dismiss actionError after 8s
  useEffect(() => {
    if (!actionError) return;
    const t = setTimeout(() => setActionError(null), 8_000);
    return () => clearTimeout(t);
  }, [actionError]);

  // Auto-dismiss provStatus after 10s
  useEffect(() => {
    if (provStatus !== "done") return;
    const t = setTimeout(() => setProvStatus("idle"), 10_000);
    return () => clearTimeout(t);
  }, [provStatus]);

  const clusterNameValid = CLUSTER_NAME_RE.test(clusterName);
  const estimatedCost = (SKU_INFO[nodeSku]?.costPerNode ?? 1.34) * nodeCount;


  const formatTime = (s: number) => `${Math.floor(s / 60)}m ${s % 60}s`;

  const status = !enabled
    ? "idle"
    : query.isLoading
      ? "loading"
      : query.isError
        ? "error"
        : noClusters
          ? "not-provisioned"
          : "ok";

  return (
    <MonitorCard
      title="AKS Cluster"
      subtitle={enabled ? resourceGroup : "Configure subscription / RG"}
      status={provStatus === "creating" ? "loading" : status}
      lastRefreshed={query.dataUpdatedAt ? new Date(query.dataUpdatedAt) : null}
      refreshCountdown={useRefreshCountdown(query.dataUpdatedAt, 30_000)}
      refreshInterval={30_000}
      onRefresh={() => { setActionError(null); setProvError(null); query.refetch(); }}
      accentColor="cluster"
      collapsible
      rightSlot={
        enabled && noClusters && !showProvision && (
          <button
            className="glass-button glass-button--primary"
            onClick={() => setShowProvision(true)}
            style={{ fontSize: 11 }}
          >
            <Plus size={12} strokeWidth={1.5} /> Create Cluster
          </button>
        )
      }
    >
      {!enabled && <div className="muted">Set Subscription ID and Workload RG above.</div>}
      {query.isError && <div className="muted">Failed: {(query.error as Error).message}</div>}
      {query.data?.clusters.length === 0 && !showProvision && provStatus !== "creating" && provStatus !== "done" && (
        <div className="muted">No AKS clusters found. Click "Create Cluster" to provision one.</div>
      )}

      {/* Provision form */}
      {showProvision && (
        <div style={{ padding: "var(--space-3)", border: "1px solid var(--glass-border)", borderRadius: 8, marginBottom: "var(--space-3)" }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: "var(--space-3)" }}>Create AKS Cluster</div>
          <div style={{ display: "grid", gap: "var(--space-3)" }}>
            <div>
              <label style={{ fontSize: 11, color: "var(--text-muted)" }}>Cluster Name</label>
              <input
                type="text"
                value={clusterName}
                onChange={(e) => setClusterName(e.target.value)}
                style={{ width: "100%", background: "var(--glass-bg)", border: "1px solid var(--glass-border)", borderRadius: 6, padding: "6px 10px", color: "var(--text-primary)", fontSize: 13 }}
                placeholder="elb-cluster"
              />
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-3)" }}>
              <div>
                <label style={{ fontSize: 11, color: "var(--text-muted)" }}>Node SKU (E16–E64)</label>
                <select
                  value={nodeSku}
                  onChange={(e) => setNodeSku(e.target.value)}
                  style={{ width: "100%", background: "var(--glass-bg)", border: "1px solid var(--glass-border)", borderRadius: 6, padding: "6px 10px", color: "var(--text-primary)", fontSize: 13 }}
                >
                  {(skuQuery.data?.skus || [DEFAULT_SKU]).map((s) => (
                    <option key={s} value={s}>{s} — {SKU_INFO[s]?.desc || ""}</option>
                  ))}
                </select>
              </div>
              <div>
                <label style={{ fontSize: 11, color: "var(--text-muted)" }}>Node Count (3–20)</label>
                <input
                  type="number"
                  min={3}
                  max={20}
                  value={nodeCount}
                  onChange={(e) => setNodeCount(Math.max(3, Math.min(20, parseInt(e.target.value) || 3)))}
                  style={{ width: "100%", background: "var(--glass-bg)", border: "1px solid var(--glass-border)", borderRadius: 6, padding: "6px 10px", color: "var(--text-primary)", fontSize: 13 }}
                />
              </div>
            </div>
            <div className="muted" style={{ fontSize: 11 }}>
              Region: <strong>{region || "not set"}</strong> · Est. cost: ~${estimatedCost.toFixed(2)}/hr
              {!region && <span style={{ color: "var(--danger)", marginLeft: 8 }}>Region required</span>}
            </div>
            {!clusterNameValid && clusterName.length > 0 && (
              <div style={{ fontSize: 10, color: "var(--danger)" }}>
                Name must start with a letter, contain only letters/digits/hyphens, 2–63 chars.
              </div>
            )}
            <div style={{ display: "flex", gap: "var(--space-2)" }}>
              <button
                className="glass-button glass-button--primary"
                onClick={handleProvision}
                disabled={provStatus === "creating" || !region || !clusterNameValid}
                style={{ fontSize: 11 }}
              >
                {provStatus === "creating" ? <><Loader2 size={12} className="spin" /> Creating...</> : "Create Cluster"}
              </button>
              <button className="glass-button" onClick={() => setShowProvision(false)} style={{ fontSize: 11 }}>Cancel</button>
            </div>
          </div>
        </div>
      )}

      {/* Creating status */}
      {provStatus === "creating" && (
        <div style={{ padding: "8px 12px", background: "rgba(110,159,255,0.06)", border: "1px solid rgba(110,159,255,0.15)", borderRadius: 6, fontSize: 12, color: "var(--accent)", marginBottom: "var(--space-3)" }}>
          <Loader2 size={12} className="spin" style={{ display: "inline", verticalAlign: "middle", marginRight: 6 }} />
          Creating AKS cluster <strong>{clusterName}</strong>... Elapsed: {formatTime(elapsed)} · Est. 5-10 minutes
        </div>
      )}
      {provStatus === "done" && (
        <div style={{ fontSize: 12, color: "var(--success)", marginBottom: "var(--space-3)" }}>
          <CheckCircle2 size={12} style={{ verticalAlign: "middle" }} /> Cluster created in {formatTime(elapsed)}.
          {roleResult && roleResult.length > 0 && (
            <span> Roles auto-assigned: {roleResult.join(", ")}</span>
          )}
        </div>
      )}
      {provError && (
        <div style={{ fontSize: 12, color: "var(--danger)", marginBottom: "var(--space-3)" }}>
          <AlertTriangle size={12} style={{ verticalAlign: "middle" }} /> {provError}
        </div>
      )}

      {/* Existing clusters */}
      <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "grid", gap: "var(--space-3)" }}>
        {query.data?.clusters.map((c) => (
          <li key={c.name} className="glass-card" style={{ padding: "var(--space-3)" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <strong>{c.name}</strong>
              <div style={{ display: "flex", gap: "var(--space-2)", alignItems: "center" }}>
                <span className="muted" style={{ fontSize: 12 }}>
                  {c.region} · {c.k8s_version ?? "?"}
                </span>
                {/* Start/Stop button — respect transition state */}
                {(() => {
                  const trans = transitioning.get(c.name);
                  if (trans === "starting") {
                    return (
                      <span style={{ fontSize: 10, color: "var(--accent)", display: "flex", alignItems: "center", gap: 4 }}>
                        <Loader2 size={10} className="spin" /> Starting...
                      </span>
                    );
                  }
                  if (trans === "stopping") {
                    return (
                      <span style={{ fontSize: 10, color: "var(--warning)", display: "flex", alignItems: "center", gap: 4 }}>
                        <Loader2 size={10} className="spin" /> Stopping...
                      </span>
                    );
                  }
                  if (c.power_state === "Stopped") {
                    return (
                      <button
                        className="glass-button"
                        onClick={() => handleStartStop(c.name, "start")}
                        disabled={actionLoading !== null}
                        style={{ fontSize: 10, padding: "2px 8px", color: "var(--success)" }}
                        title="Start cluster"
                      >
                        {actionLoading === `start-${c.name}` ? <Loader2 size={10} className="spin" /> : <Play size={10} strokeWidth={1.5} />}
                        {" "}Start
                      </button>
                    );
                  }
                  if (c.power_state === "Running") {
                    return (
                      <button
                        className="glass-button"
                        onClick={() => handleStartStop(c.name, "stop")}
                        disabled={actionLoading !== null}
                        style={{ fontSize: 10, padding: "2px 8px", color: "var(--warning)" }}
                        title="Stop cluster (saves cost)"
                      >
                        {actionLoading === `stop-${c.name}` ? <Loader2 size={10} className="spin" /> : <Square size={10} strokeWidth={1.5} />}
                        {" "}Stop
                      </button>
                    );
                  }
                  return null;
                })()}
                <button
                  className="glass-button"
                  onClick={() => setDeleteTarget(c.name)}
                  disabled={actionLoading !== null}
                  style={{ fontSize: 10, padding: "2px 8px", color: "var(--danger)" }}
                  title="Delete cluster"
                >
                  {actionLoading === `delete-${c.name}` ? <Loader2 size={10} className="spin" /> : <Trash2 size={10} strokeWidth={1.5} />}
                </button>
              </div>
            </div>
            <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
              Power: {(() => {
                const trans = transitioning.get(c.name);
                if (trans === "starting") return <span style={{ color: "var(--accent)" }}>Starting...</span>;
                if (trans === "stopping") return <span style={{ color: "var(--warning)" }}>Stopping...</span>;
                return <span style={{ color: c.power_state === "Running" ? "var(--success)" : "var(--warning)" }}>{c.power_state ?? "?"}</span>;
              })()}
              {" · "}State: {c.provisioning_state ?? "?"}
              {" · "}Nodes: {c.node_count ?? "?"} {c.node_sku ? `(${c.node_sku})` : ""}
            </div>
            {c.kubelet_object_id && (
              <div className="muted" style={{ fontSize: 11, marginTop: 2, display: "flex", alignItems: "center", gap: 4 }}>
                Kubelet OID: <code style={{ fontSize: 10 }}>{c.kubelet_object_id}</code>
                <button
                  className="glass-button"
                  style={{ padding: "1px 4px", border: "none", opacity: 0.6 }}
                  onClick={() => navigator.clipboard.writeText(c.kubelet_object_id!)}
                  title="Copy OID"
                >
                  <Copy size={9} />
                </button>
              </div>
            )}
            {/* Cluster details section */}
            <ClusterDetails
              clusterName={c.name}
              powerState={c.power_state}
              agentPools={c.agent_pools}
              fqdn={c.fqdn}
              networkPlugin={c.network_plugin}
              subscriptionId={subscriptionId}
              resourceGroup={resourceGroup}
            />
          </li>
        ))}
      </ul>

      {actionError && (
        <div style={{ marginTop: "var(--space-2)", fontSize: 11, color: "var(--danger)" }}>
          <AlertTriangle size={10} style={{ verticalAlign: "middle" }} /> {actionError}
        </div>
      )}

      {deleteTarget && (
        <ConfirmDialog
          title={`Delete cluster "${deleteTarget}"?`}
          message="This action is irreversible. The cluster and all its workloads will be permanently deleted."
          confirmLabel="Delete"
          onConfirm={() => handleDelete(deleteTarget)}
          onCancel={() => setDeleteTarget(null)}
        />
      )}
    </MonitorCard>
  );
}

// ---------------------------------------------------------------------------
// Cluster Details — compact inline summary + modal for full details
// ---------------------------------------------------------------------------
import type { AksAgentPool } from "@/api/endpoints";

function ClusterDetails({
  clusterName,
  powerState,
  agentPools,
  fqdn,
  networkPlugin,
  subscriptionId,
  resourceGroup,
}: {
  clusterName: string;
  powerState: string | null;
  agentPools?: AksAgentPool[];
  fqdn?: string | null;
  networkPlugin?: string | null;
  subscriptionId: string;
  resourceGroup: string;
}) {
  const isRunning = powerState === "Running";
  const [showModal, setShowModal] = useState(false);

  // Fast K8s metrics API — direct access (~1-3s instead of ~30s)
  const topQuery = useQuery({
    queryKey: ["aks-top-fast", subscriptionId, resourceGroup, clusterName],
    queryFn: () => monitoringApi.k8sTopNodes(subscriptionId, resourceGroup, clusterName),
    enabled: isRunning,
    staleTime: 30_000,
    refetchInterval: isRunning ? 60_000 : false,
  });

  const nodeMetrics = (topQuery.data?.nodes ?? []).map((n) => {
    const short = n.name.replace(/^aks-/, "").replace(/-vmss\d+$/, "");
    return { name: short, fullName: n.name, cpu: n.cpu, cpuPct: `${n.cpu_pct}%`, mem: n.memory, memPct: `${n.memory_pct}%` };
  });

  // ESC + body scroll lock for modal
  useEffect(() => {
    if (!showModal) return;
    const handleEsc = (e: KeyboardEvent) => { if (e.key === "Escape") setShowModal(false); };
    window.addEventListener("keydown", handleEsc);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { window.removeEventListener("keydown", handleEsc); document.body.style.overflow = prev; };
  }, [showModal]);

  return (
    <div style={{ marginTop: "var(--space-2)" }}>
      {/* Compact inline: node CPU/Memory bars */}
      {isRunning && nodeMetrics.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 4, marginTop: 4 }}>
          <div className="muted" style={{ fontSize: 9, textTransform: "uppercase", display: "flex", justifyContent: "space-between" }}>
            <span>Node Resources</span>
            {topQuery.isFetching && <Loader2 size={9} className="spin" />}
          </div>
          {nodeMetrics.map((n) => (
            <div key={n.fullName} style={{ fontSize: 10, display: "grid", gridTemplateColumns: "1fr 70px 70px", gap: 6, alignItems: "center" }}>
              <span className="muted" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={n.fullName}>{n.name}</span>
              <div style={{ display: "flex", alignItems: "center", gap: 3 }}>
                <div style={{ flex: 1, height: 4, background: "var(--bg-tertiary)", borderRadius: 2, overflow: "hidden" }}>
                  <div style={{ width: n.cpuPct, height: "100%", background: "var(--accent)", borderRadius: 2 }} />
                </div>
                <span style={{ fontSize: 9, color: "var(--text-faint)", minWidth: 28, textAlign: "right" }}>{n.cpuPct}</span>
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 3 }}>
                <div style={{ flex: 1, height: 4, background: "var(--bg-tertiary)", borderRadius: 2, overflow: "hidden" }}>
                  <div style={{ width: n.memPct, height: "100%", background: "var(--purple)", borderRadius: 2 }} />
                </div>
                <span style={{ fontSize: 9, color: "var(--text-faint)", minWidth: 28, textAlign: "right" }}>{n.memPct}</span>
              </div>
            </div>
          ))}
          <div className="muted" style={{ fontSize: 8, display: "flex", gap: 12 }}>
            <span><span style={{ display: "inline-block", width: 6, height: 6, borderRadius: 1, background: "var(--accent)", verticalAlign: "middle", marginRight: 3 }} />CPU</span>
            <span><span style={{ display: "inline-block", width: 6, height: 6, borderRadius: 1, background: "var(--purple)", verticalAlign: "middle", marginRight: 3 }} />Memory</span>
          </div>
        </div>
      )}

      {isRunning && topQuery.isLoading && nodeMetrics.length === 0 && (
        <div className="muted" style={{ fontSize: 10, marginTop: 4, display: "flex", alignItems: "center", gap: 4 }}>
          <Loader2 size={10} className="spin" /> Loading node metrics...
        </div>
      )}

      {!isRunning && (
        <div className="muted" style={{ fontSize: 10, marginTop: 4 }}>
          Start the cluster to view node metrics.
        </div>
      )}

      {/* Open modal button */}
      <button
        onClick={() => setShowModal(true)}
        style={{
          display: "flex", alignItems: "center", gap: 4, marginTop: 6,
          background: "none", border: "none", color: "var(--accent)",
          cursor: "pointer", padding: 0, fontSize: 10,
        }}
      >
        <Maximize2 size={10} /> View full details
      </button>

      {/* Full details modal */}
      {showModal && createPortal(
        <div
          className="glass-dialog-backdrop"
          onClick={(e) => { if (e.target === e.currentTarget) setShowModal(false); }}
          role="dialog"
          aria-modal="true"
          aria-label={`${clusterName} Details`}
        >
          <div
            className="glass-card glass-card--strong glass-dialog"
            onClick={(e) => e.stopPropagation()}
            style={{ maxWidth: 780, width: "94vw", maxHeight: "88vh", display: "flex", flexDirection: "column", padding: 0, overflow: "hidden" }}
          >
            {/* ── Premium header with accent gradient ── */}
            <div style={{
              padding: "20px 24px 16px",
              background: "linear-gradient(135deg, rgba(110,159,255,0.08) 0%, rgba(184,119,217,0.06) 100%)",
              borderBottom: "1px solid var(--border-weak)",
            }}>
              <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between" }}>
                <div>
                  <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                    <div style={{
                      width: 36, height: 36, borderRadius: 10,
                      background: "linear-gradient(135deg, var(--accent), var(--purple))",
                      display: "flex", alignItems: "center", justifyContent: "center",
                      boxShadow: "0 4px 12px rgba(110,159,255,0.25)",
                    }}>
                      <span style={{ fontSize: 16 }}>⎈</span>
                    </div>
                    <div>
                      <h3 style={{ margin: 0, fontSize: 18, fontWeight: 700, letterSpacing: "-0.02em" }}>{clusterName}</h3>
                      <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 2 }}>
                        <span style={{
                          display: "inline-flex", alignItems: "center", gap: 4,
                          fontSize: 11, fontWeight: 600,
                          color: powerState === "Running" ? "var(--success)" : "var(--warning)",
                        }}>
                          <span style={{
                            width: 6, height: 6, borderRadius: "50%",
                            background: powerState === "Running" ? "var(--success)" : "var(--warning)",
                            boxShadow: powerState === "Running" ? "0 0 8px var(--success)" : "none",
                            animation: powerState === "Running" ? "blink 1.8s ease-in-out infinite" : "none",
                          }} />
                          {powerState ?? "Unknown"}
                        </span>
                        {fqdn && <span className="muted" style={{ fontSize: 10 }}>·</span>}
                        {fqdn && <code style={{ fontSize: 9, color: "var(--text-faint)", background: "rgba(255,255,255,0.04)", padding: "2px 6px", borderRadius: 4 }}>{fqdn}</code>}
                      </div>
                    </div>
                  </div>
                </div>
                <button
                  className="glass-button"
                  onClick={() => setShowModal(false)}
                  style={{ padding: "6px 8px", border: "none", background: "rgba(255,255,255,0.05)" }}
                  title="Close (Esc)"
                >
                  <X size={16} strokeWidth={1.5} />
                </button>
              </div>

              {/* ── Stat cards row ── */}
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(100px, 1fr))", gap: 10, marginTop: 16 }}>
                {[
                  { label: "Nodes", value: agentPools?.[0]?.count ?? "—", sub: agentPools?.[0]?.vm_size ?? "" },
                  { label: "K8s", value: networkPlugin ?? "—", sub: "network" },
                  { label: "Pools", value: String(agentPools?.length ?? 0), sub: agentPools?.map(p => p.name).join(", ") ?? "" },
                  { label: "OS", value: agentPools?.[0]?.os_type ?? "—", sub: agentPools?.[0]?.mode ?? "" },
                ].map((s) => (
                  <div key={s.label} style={{
                    padding: "10px 12px", borderRadius: 8,
                    background: "rgba(255,255,255,0.03)", border: "1px solid var(--border-weak)",
                  }}>
                    <div className="muted" style={{ fontSize: 9, textTransform: "uppercase", letterSpacing: "0.06em" }}>{s.label}</div>
                    <div style={{ fontSize: 16, fontWeight: 700, marginTop: 2, letterSpacing: "-0.02em" }}>{s.value}</div>
                    <div className="muted" style={{ fontSize: 9, marginTop: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{s.sub}</div>
                  </div>
                ))}
              </div>
            </div>

            {/* ── Scrollable body ── */}
            <div style={{ overflowY: "auto", flex: 1, padding: "16px 24px 24px" }}>

              {/* ── Node Pools table ── */}
              {agentPools && agentPools.length > 0 && (
                <div style={{ marginBottom: 20 }}>
                  <div style={{ fontSize: 11, fontWeight: 600, marginBottom: 8, display: "flex", alignItems: "center", gap: 6 }}>
                    <span style={{ width: 3, height: 14, borderRadius: 2, background: "var(--accent)" }} />
                    Node Pools
                  </div>
                  <div style={{ borderRadius: 8, border: "1px solid var(--border-weak)", overflow: "hidden" }}>
                    <table style={{ width: "100%", fontSize: 11, borderCollapse: "collapse" }}>
                      <thead>
                        <tr style={{ background: "var(--bg-tertiary)" }}>
                          {["Pool", "SKU", "Nodes", "OS", "Mode", "Autoscale", "State"].map((h) => (
                            <th key={h} style={{ textAlign: h === "Nodes" ? "center" : "left", padding: "8px 10px", color: "var(--text-faint)", fontSize: 9, textTransform: "uppercase", letterSpacing: "0.05em", fontWeight: 500 }}>{h}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {agentPools.map((p, i) => (
                          <tr key={p.name} style={{ background: i % 2 === 0 ? "transparent" : "rgba(255,255,255,0.015)", borderTop: "1px solid var(--border-weak)" }}>
                            <td style={{ padding: "8px 10px", fontWeight: 600 }}>{p.name}</td>
                            <td style={{ padding: "8px 10px" }}><code style={{ fontSize: 10 }}>{p.vm_size}</code></td>
                            <td style={{ padding: "8px 10px", textAlign: "center", fontWeight: 600 }}>{p.count}</td>
                            <td style={{ padding: "8px 10px" }}>{p.os_type}</td>
                            <td style={{ padding: "8px 10px" }}>
                              <span style={{ fontSize: 9, padding: "2px 6px", borderRadius: 4, background: p.mode === "System" ? "rgba(110,159,255,0.1)" : "rgba(115,191,105,0.1)", color: p.mode === "System" ? "var(--accent)" : "var(--success)" }}>
                                {p.mode}
                              </span>
                            </td>
                            <td style={{ padding: "8px 10px", fontSize: 10 }}>
                              {p.enable_auto_scaling ? <span style={{ color: "var(--success)" }}>{p.min_count}–{p.max_count}</span> : <span className="muted">Off</span>}
                            </td>
                            <td style={{ padding: "8px 10px" }}>
                              <span style={{
                                display: "inline-flex", alignItems: "center", gap: 4, fontSize: 10, fontWeight: 500,
                                color: p.power_state === "Running" ? "var(--success)" : "var(--warning)",
                              }}>
                                <span style={{ width: 5, height: 5, borderRadius: "50%", background: p.power_state === "Running" ? "var(--success)" : "var(--warning)" }} />
                                {p.power_state ?? "?"}
                              </span>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}

              {/* kubectl sections — only when running */}
              {!isRunning && (
                <div style={{
                  padding: "20px", borderRadius: 8, textAlign: "center",
                  background: "rgba(255,255,255,0.02)", border: "1px dashed var(--border-weak)",
                  color: "var(--text-faint)", fontSize: 12,
                }}>
                  Start the cluster to view diagnostics and run kubectl commands.
                </div>
              )}

              {isRunning && (
                <ClusterModalKubectl
                  subscriptionId={subscriptionId}
                  resourceGroup={resourceGroup}
                  clusterName={clusterName}
                  topQuery={topQuery}
                />
              )}
            </div>
          </div>
        </div>,
        document.body,
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Modal kubectl sections (fetched on mount)
// ---------------------------------------------------------------------------
function ClusterModalKubectl({
  subscriptionId,
  resourceGroup,
  clusterName,
  topQuery,
}: {
  subscriptionId: string;
  resourceGroup: string;
  clusterName: string;
  topQuery: { isLoading: boolean; isError: boolean; data?: { nodes: K8sNodeMetrics[] } | null; error?: unknown; refetch: () => void };
}) {
  const nodesQuery = useQuery({
    queryKey: ["aks-nodes-fast", subscriptionId, resourceGroup, clusterName],
    queryFn: () => monitoringApi.k8sNodes(subscriptionId, resourceGroup, clusterName),
    staleTime: 60_000,
  });

  const podsQuery = useQuery({
    queryKey: ["aks-pods-fast", subscriptionId, resourceGroup, clusterName],
    queryFn: () => monitoringApi.k8sPods(subscriptionId, resourceGroup, clusterName),
    staleTime: 60_000,
  });

  const [customCmd, setCustomCmd] = useState("");
  const [customResult, setCustomResult] = useState<{ output: string; exit_code: number } | null>(null);
  const [customLoading, setCustomLoading] = useState(false);

  const runCustom = useCallback(async () => {
    if (!customCmd.trim()) return;
    setCustomLoading(true);
    try {
      const result = await monitoringApi.runAksCommand(subscriptionId, resourceGroup, clusterName, customCmd.trim());
      setCustomResult(result);
    } catch (e) {
      setCustomResult({ output: (e as Error).message, exit_code: -1 });
    } finally {
      setCustomLoading(false);
    }
  }, [customCmd, subscriptionId, resourceGroup, clusterName]);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {/* Section header */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <div style={{ fontSize: 11, fontWeight: 600, display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ width: 3, height: 14, borderRadius: 2, background: "var(--teal)" }} />
          Cluster Diagnostics
        </div>
        <button
          className="glass-button"
          onClick={() => { topQuery.refetch(); nodesQuery.refetch(); podsQuery.refetch(); }}
          style={{ padding: "4px 10px", fontSize: 10, display: "flex", alignItems: "center", gap: 4 }}
          title="Refresh all diagnostics"
        >
          <RefreshCw size={10} strokeWidth={1.5} /> Refresh All
        </button>
      </div>

      {/* Node Resources — fast K8s metrics API */}
      <NodeResourcesSection query={topQuery} />

      {/* Nodes — fast direct API */}
      <K8sNodesSection query={nodesQuery} />

      {/* Active Pods — fast direct API with logs */}
      <K8sPodsSection query={podsQuery}
        subscriptionId={subscriptionId} resourceGroup={resourceGroup} clusterName={clusterName}
      />

      {/* Custom command */}
      <div style={{ borderRadius: 8, border: "1px solid var(--border-weak)", overflow: "hidden" }}>
        <div style={{
          padding: "8px 12px", background: "var(--bg-tertiary)",
          fontSize: 10, fontWeight: 500, display: "flex", alignItems: "center", gap: 6,
          borderBottom: "1px solid var(--border-weak)",
        }}>
          <Terminal size={12} strokeWidth={1.5} /> Run kubectl command
          <span className="muted" style={{ fontSize: 9, marginLeft: "auto" }}>read-only: get, top, describe, logs</span>
        </div>
        <div style={{ padding: "10px 12px", display: "flex", gap: 8 }}>
          <input
            type="text"
            value={customCmd}
            onChange={(e) => setCustomCmd(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") runCustom(); }}
            placeholder="kubectl get svc -A"
            style={{
              flex: 1, fontSize: 12, padding: "7px 10px",
              background: "var(--bg-canvas)", border: "1px solid var(--border-weak)",
              borderRadius: 6, color: "var(--text-primary)", fontFamily: "var(--font-mono)",
            }}
            spellCheck={false}
          />
          <button
            className="glass-button glass-button--primary"
            onClick={runCustom}
            disabled={customLoading || !customCmd.trim()}
            style={{ fontSize: 10, padding: "4px 10px" }}
          >
            {customLoading ? <Loader2 size={10} className="spin" /> : "Run"}
          </button>
        </div>
        {customResult && (
          <pre style={{
            margin: 0, padding: "10px 12px", fontSize: 11, lineHeight: 1.5,
            background: "var(--bg-canvas)", borderTop: "1px solid var(--border-weak)",
            overflow: "auto", maxHeight: 250, whiteSpace: "pre-wrap", wordBreak: "break-all",
            color: customResult.exit_code === 0 ? "var(--text-primary)" : "var(--danger)",
            fontFamily: "var(--font-mono)",
          }}>
            {customResult.output || "(no output)"}
          </pre>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Node Resources — visual progress bars (typed K8s metrics API)
// ---------------------------------------------------------------------------
import type { K8sNodeMetrics, K8sNode, K8sPod } from "@/api/endpoints";

function NodeResourcesSection({ query }: { query: { isLoading: boolean; isError: boolean; data?: { nodes: K8sNodeMetrics[] } | null; error?: unknown } }) {
  const metrics = query.data?.nodes ?? [];

  const shortName = (n: string) => n.replace(/^aks-/, "").replace(/-vmss/, "-");

  return (
    <div style={{ borderRadius: 8, border: "1px solid var(--border-weak)", overflow: "hidden" }}>
      <div style={{
        padding: "8px 12px", background: "var(--bg-tertiary)",
        fontSize: 11, fontWeight: 500, display: "flex", alignItems: "center", gap: 6,
        borderBottom: "1px solid var(--border-weak)",
      }}>
        Node Resources
        {query.isLoading && <Loader2 size={10} className="spin" style={{ marginLeft: "auto", color: "var(--accent)" }} />}
        {!query.isLoading && metrics.length > 0 && <span style={{ marginLeft: "auto", fontSize: 9, color: "var(--success)" }}>✓</span>}
      </div>
      <div style={{ padding: "12px 14px" }}>
        {query.isLoading && metrics.length === 0 && (
          <div className="muted" style={{ fontSize: 11, display: "flex", alignItems: "center", gap: 6 }}>
            <Loader2 size={12} className="spin" /> Fetching node metrics...
          </div>
        )}
        {query.isError && (
          <div style={{ fontSize: 11, color: "var(--danger)" }}>Failed to load: {(query.error as Error)?.message}</div>
        )}
        {metrics.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {/* Header */}
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, paddingLeft: 140 }}>
              <div className="muted" style={{ fontSize: 9, textTransform: "uppercase", letterSpacing: "0.05em", display: "flex", alignItems: "center", gap: 4 }}>
                <span style={{ width: 8, height: 8, borderRadius: 2, background: "var(--accent)" }} /> CPU
              </div>
              <div className="muted" style={{ fontSize: 9, textTransform: "uppercase", letterSpacing: "0.05em", display: "flex", alignItems: "center", gap: 4 }}>
                <span style={{ width: 8, height: 8, borderRadius: 2, background: "var(--purple)" }} /> Memory
              </div>
            </div>
            {metrics.map((n) => (
              <div key={n.name} style={{ display: "grid", gridTemplateColumns: "140px 1fr 1fr", gap: 16, alignItems: "center" }}>
                <span style={{ fontSize: 10, fontFamily: "var(--font-mono)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={n.name}>
                  {shortName(n.name)}
                </span>
                {/* CPU bar */}
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <div style={{ flex: 1, height: 8, background: "var(--bg-tertiary)", borderRadius: 4, overflow: "hidden", position: "relative" }}>
                    <div style={{
                      width: `${Math.max(n.cpu_pct, 2)}%`, height: "100%", borderRadius: 4,
                      background: n.cpu_pct > 80 ? "var(--danger)" : n.cpu_pct > 50 ? "var(--warning)" : "var(--accent)",
                      transition: "width 0.5s ease-out",
                    }} />
                  </div>
                  <span style={{ fontSize: 10, fontFamily: "var(--font-mono)", minWidth: 50, textAlign: "right", color: "var(--text-muted)" }}>
                    {n.cpu} <span style={{ color: "var(--text-faint)" }}>({n.cpu_pct}%)</span>
                  </span>
                </div>
                {/* Memory bar */}
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <div style={{ flex: 1, height: 8, background: "var(--bg-tertiary)", borderRadius: 4, overflow: "hidden", position: "relative" }}>
                    <div style={{
                      width: `${Math.max(n.memory_pct, 2)}%`, height: "100%", borderRadius: 4,
                      background: n.memory_pct > 80 ? "var(--danger)" : n.memory_pct > 50 ? "var(--warning)" : "var(--purple)",
                      transition: "width 0.5s ease-out",
                    }} />
                  </div>
                  <span style={{ fontSize: 10, fontFamily: "var(--font-mono)", minWidth: 60, textAlign: "right", color: "var(--text-muted)" }}>
                    {n.memory} <span style={{ color: "var(--text-faint)" }}>({n.memory_pct}%)</span>
                  </span>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// K8s Nodes Section — typed data from direct K8s API
// ---------------------------------------------------------------------------
function K8sNodesSection({ query }: { query: { isLoading: boolean; isError: boolean; data?: { nodes: K8sNode[] } | null; error?: unknown } }) {
  const [collapsed, setCollapsed] = useState(true);
  const nodes = query.data?.nodes ?? [];
  const sc = (s: string) => s === "Ready" ? "var(--success)" : "var(--danger)";
  return (
    <div style={{ borderRadius: 8, border: "1px solid var(--border-weak)", overflow: "hidden" }}>
      <button onClick={() => { setCollapsed(!collapsed); }} style={{ display: "flex", alignItems: "center", gap: 6, width: "100%", background: collapsed ? "transparent" : "var(--bg-tertiary)", border: "none", color: "var(--text-primary)", cursor: "pointer", padding: "8px 12px", fontSize: 11, textAlign: "left", fontWeight: 500 }}>
        <ChevronDown size={12} style={{ transform: collapsed ? "rotate(-90deg)" : "rotate(0deg)", color: "var(--text-faint)", transition: "transform 0.15s" }} />
        Nodes
        {nodes.length > 0 && <span className="muted" style={{ fontSize: 9 }}>{nodes.length}</span>}
        {query.isLoading && <Loader2 size={10} className="spin" style={{ marginLeft: "auto", color: "var(--accent)" }} />}
        {!query.isLoading && nodes.length > 0 && <span style={{ marginLeft: "auto", fontSize: 9, color: "var(--success)" }}>✓</span>}
      </button>
      {!collapsed && (
        <div style={{ borderTop: "1px solid var(--border-weak)", overflowX: "auto" }}>
          {query.isLoading && <div style={{ padding: 16, textAlign: "center" }} className="muted"><Loader2 size={14} className="spin" /> Loading...</div>}
          {query.isError && <div style={{ padding: 12, fontSize: 11, color: "var(--danger)" }}>Error: {(query.error as Error)?.message}</div>}
          {nodes.length > 0 && (
            <table style={{ width: "100%", fontSize: 10, borderCollapse: "collapse", fontFamily: "var(--font-mono)" }}>
              <thead><tr style={{ background: "var(--bg-tertiary)" }}>
                {["NAME","STATUS","VERSION","IP","OS","RUNTIME"].map(h=><th key={h} style={{ textAlign: "left", padding: "6px 8px", color: "var(--text-faint)", fontSize: 9, textTransform: "uppercase", fontWeight: 500 }}>{h}</th>)}
              </tr></thead>
              <tbody>{nodes.map((n,i)=>(
                <tr key={n.name} style={{ background: i%2===0 ? "transparent" : "rgba(255,255,255,0.012)", borderTop: "1px solid var(--border-weak)" }}>
                  <td style={{ padding: "5px 8px", fontWeight: 500 }}>{n.name}</td>
                  <td style={{ padding: "5px 8px", color: sc(n.status) }}><span style={{ display: "inline-block", width: 5, height: 5, borderRadius: "50%", background: sc(n.status), marginRight: 4, verticalAlign: "middle" }}/>{n.status}</td>
                  <td style={{ padding: "5px 8px" }}>{n.version}</td>
                  <td style={{ padding: "5px 8px" }}>{n.internal_ip}</td>
                  <td style={{ padding: "5px 8px" }}>{n.os_image}</td>
                  <td style={{ padding: "5px 8px" }}>{n.runtime}</td>
                </tr>
              ))}</tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// K8s Pods Section — typed data with fast log viewing
// ---------------------------------------------------------------------------
function K8sPodsSection({ query, subscriptionId, resourceGroup, clusterName }: {
  query: { isLoading: boolean; isError: boolean; data?: { pods: K8sPod[] } | null; error?: unknown };
  subscriptionId: string; resourceGroup: string; clusterName: string;
}) {
  const [collapsed, setCollapsed] = useState(true);
  const [logTarget, setLogTarget] = useState<{ namespace: string; pod: string } | null>(null);
  const [logOutput, setLogOutput] = useState<string | null>(null);
  const [logLoading, setLogLoading] = useState(false);
  const pods = query.data?.pods ?? [];
  const sc = (s: string) => { const v=s.toLowerCase(); return v==="running" ? "var(--success)" : v.includes("error")||v.includes("crash") ? "var(--danger)" : "var(--warning)"; };
  const fetchLogs = useCallback(async (ns: string, pod: string) => {
    setLogTarget({ namespace: ns, pod }); setLogOutput(null); setLogLoading(true);
    try { const r = await monitoringApi.k8sPodLogs(subscriptionId, resourceGroup, clusterName, ns, pod, 200); setLogOutput(r.logs || "(empty)"); }
    catch (e) { setLogOutput(`Error: ${(e as Error).message}`); }
    finally { setLogLoading(false); }
  }, [subscriptionId, resourceGroup, clusterName]);
  return (
    <div style={{ borderRadius: 8, border: "1px solid var(--border-weak)", overflow: "hidden" }}>
      <button onClick={() => { setCollapsed(!collapsed); }} style={{ display: "flex", alignItems: "center", gap: 6, width: "100%", background: collapsed ? "transparent" : "var(--bg-tertiary)", border: "none", color: "var(--text-primary)", cursor: "pointer", padding: "8px 12px", fontSize: 11, textAlign: "left", fontWeight: 500 }}>
        <ChevronDown size={12} style={{ transform: collapsed ? "rotate(-90deg)" : "rotate(0deg)", color: "var(--text-faint)", transition: "transform 0.15s" }} />
        Active Pods
        {pods.length > 0 && <span className="muted" style={{ fontSize: 9 }}>{pods.length}</span>}
        {query.isLoading && <Loader2 size={10} className="spin" style={{ marginLeft: "auto", color: "var(--accent)" }} />}
        {!query.isLoading && pods.length > 0 && <span style={{ marginLeft: "auto", fontSize: 9, color: "var(--success)" }}>✓</span>}
      </button>
      {!collapsed && (
        <div style={{ borderTop: "1px solid var(--border-weak)", overflowX: "auto" }}>
          {query.isLoading && <div style={{ padding: 16, textAlign: "center" }} className="muted"><Loader2 size={14} className="spin" /> Loading...</div>}
          {query.isError && <div style={{ padding: 12, fontSize: 11, color: "var(--danger)" }}>Error: {(query.error as Error)?.message}</div>}
          {pods.length > 0 && (
            <table style={{ width: "100%", fontSize: 10, borderCollapse: "collapse", fontFamily: "var(--font-mono)" }}>
              <thead><tr style={{ background: "var(--bg-tertiary)" }}>
                {["NS","NAME","READY","STATUS","RESTARTS","NODE",""].map(h=><th key={h} style={{ textAlign: "left", padding: "6px 8px", color: "var(--text-faint)", fontSize: 9, textTransform: "uppercase", fontWeight: 500 }}>{h}</th>)}
              </tr></thead>
              <tbody>{pods.map((p,i)=>(
                <tr key={`${p.namespace}/${p.name}`} style={{ background: i%2===0 ? "transparent" : "rgba(255,255,255,0.012)", borderTop: "1px solid var(--border-weak)" }}>
                  <td style={{ padding: "5px 8px", color: "var(--text-muted)", fontSize: 9 }}>{p.namespace}</td>
                  <td style={{ padding: "5px 8px", fontWeight: 500 }}>{p.name}</td>
                  <td style={{ padding: "5px 8px" }}>{p.ready}</td>
                  <td style={{ padding: "5px 8px", color: sc(p.status) }}><span style={{ display: "inline-block", width: 5, height: 5, borderRadius: "50%", background: sc(p.status), marginRight: 4, verticalAlign: "middle" }}/>{p.status}</td>
                  <td style={{ padding: "5px 8px" }}>{p.restarts}</td>
                  <td style={{ padding: "5px 8px", color: "var(--text-muted)", fontSize: 9 }}>{p.node?.split("-vmss")[0]}</td>
                  <td style={{ padding: "4px 8px" }}><button className="glass-button" onClick={()=>fetchLogs(p.namespace,p.name)} style={{ fontSize: 9, padding: "2px 6px", display: "flex", alignItems: "center", gap: 3 }} title={`Logs: ${p.name}`}><Terminal size={9}/> Logs</button></td>
                </tr>
              ))}</tbody>
            </table>
          )}
        </div>
      )}
      {logTarget && createPortal(
        <div className="glass-dialog-backdrop" onClick={(e)=>{if(e.target===e.currentTarget){setLogTarget(null);setLogOutput(null);}}} role="dialog" aria-modal="true" aria-label={`Logs: ${logTarget.pod}`}>
          <div className="glass-card glass-card--strong glass-dialog" onClick={(e)=>e.stopPropagation()} style={{ maxWidth: 820, width: "94vw", maxHeight: "85vh", display: "flex", flexDirection: "column", padding: 0, overflow: "hidden", textAlign: "left" }}>
            <div style={{ padding: "14px 20px", background: "linear-gradient(135deg, rgba(92,202,180,0.08) 0%, rgba(110,159,255,0.06) 100%)", borderBottom: "1px solid var(--border-weak)", display: "flex", alignItems: "center", justifyContent: "space-between" }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <div style={{ width: 28, height: 28, borderRadius: 8, background: "linear-gradient(135deg, var(--teal), var(--accent))", display: "flex", alignItems: "center", justifyContent: "center", boxShadow: "0 2px 8px rgba(92,202,180,0.25)" }}><Terminal size={14} style={{ color: "#fff" }}/></div>
                <div><div style={{ fontSize: 13, fontWeight: 600 }}>Pod Logs</div><div style={{ fontSize: 10, color: "var(--text-muted)" }}>{logTarget.namespace} / {logTarget.pod} · last 200 lines</div></div>
              </div>
              <div style={{ display: "flex", gap: 6 }}>
                <button className="glass-button" onClick={()=>fetchLogs(logTarget.namespace,logTarget.pod)} disabled={logLoading} style={{ padding: "5px 10px", fontSize: 10, display: "flex", alignItems: "center", gap: 4 }}><RefreshCw size={11} className={logLoading?"spin":""}/> Refresh</button>
                <button className="glass-button" onClick={()=>{setLogTarget(null);setLogOutput(null);}} style={{ padding: "5px 8px", border: "none" }}><X size={16}/></button>
              </div>
            </div>
            <div style={{ margin: 0, padding: "14px 20px", flex: 1, overflow: "auto", fontSize: 11, lineHeight: 1.7, background: "#0d1117", fontFamily: "var(--font-mono)", color: "#c9d1d9", textAlign: "left" }}>
              {logLoading ? <span style={{ color: "var(--text-faint)" }}>Fetching logs...</span> : <LogHighlighter text={logOutput ?? ""} />}
            </div>
          </div>
        </div>, document.body,
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Log syntax highlighter — lnav-style colorized log output
// ---------------------------------------------------------------------------
const LOG_COLORS = {
  timestamp: "#6cb6ff",   // blue — ISO dates, timestamps
  error:     "#f47067",   // red — ERROR, FATAL, CRITICAL, panic, fail
  warn:      "#f0c674",   // yellow — WARN, WARNING
  info:      "#57ab5a",   // green — INFO
  debug:     "#986ee2",   // purple — DEBUG, TRACE
  number:    "#d2a8ff",   // light purple — numbers, durations
  ip:        "#6cb6ff",   // blue — IP addresses
  path:      "#96d0ff",   // light blue — file paths
  key:       "#e3b341",   // golden — key= patterns
  string:    "#a5d6ff",   // cyan — quoted strings
  dim:       "#545d68",   // dim — separators, brackets
} as const;

function LogHighlighter({ text }: { text: string }) {
  if (!text) return <span style={{ color: "var(--text-faint)" }}>(empty log)</span>;

  const lines = text.split("\n");
  return (
    <>
      {lines.map((line, i) => (
        <div key={i} style={{ minHeight: "1.7em", display: "flex" }}>
          <span style={{ color: LOG_COLORS.dim, userSelect: "none", minWidth: 36, textAlign: "right", paddingRight: 12, fontSize: 9, lineHeight: "1.7em" }}>
            {i + 1}
          </span>
          <span style={{ whiteSpace: "pre-wrap", wordBreak: "break-all", flex: 1 }}>
            {highlightLine(line)}
          </span>
        </div>
      ))}
    </>
  );
}

// Pre-compiled regex patterns for log highlighting (avoid re-creation per line)
const _LOG_ERROR_RE = /\b(error|fatal|critical|panic|exception|fail(ed|ure)?)\b/i;
const _LOG_WARN_RE = /\b(warn(ing)?)\b/i;
const _LOG_TOKEN_RE = /(\d{4}[-/]\d{2}[-/]\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)|(\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?::\d+)?\b)|(\b(?:ERROR|FATAL|CRITICAL|PANIC|EXCEPTION)\b)|(\b(?:WARN(?:ING)?)\b)|(\b(?:INFO)\b)|(\b(?:DEBUG|TRACE)\b)|("[^"]*"|'[^']*')|(\/[\w./\-]+(?:\.\w+))|(\b\w+(?:[-_]\w+)*=)|(\b\d+(?:\.\d+)?(?:m|Mi|Gi|Ki|ms|s|%|ns|us|µs)?\b)/gi;

function highlightLine(line: string): React.ReactNode[] {
  // Detect log level for full-line tinting
  const isError = _LOG_ERROR_RE.test(line);
  const isWarn = !isError && _LOG_WARN_RE.test(line);

  // Tokenize with pre-compiled regex (reset lastIndex for global regex)
  const pattern = _LOG_TOKEN_RE;
  pattern.lastIndex = 0;

  const parts: React.ReactNode[] = [];
  let lastIdx = 0;
  let match: RegExpExecArray | null;
  let key = 0;

  while ((match = pattern.exec(line)) !== null) {
    // Push text before match
    if (match.index > lastIdx) {
      const before = line.slice(lastIdx, match.index);
      parts.push(<span key={key++} style={isError ? { color: "#ffa198" } : isWarn ? { color: "#e3b341", opacity: 0.85 } : undefined}>{before}</span>);
    }

    const [fullMatch, ts, ip, err, warn, info, debug, str, path, kv, num] = match;
    let color = "#c9d1d9";
    let fontWeight: number | undefined;

    if (ts) color = LOG_COLORS.timestamp;
    else if (ip) color = LOG_COLORS.ip;
    else if (err) { color = LOG_COLORS.error; fontWeight = 700; }
    else if (warn) { color = LOG_COLORS.warn; fontWeight = 600; }
    else if (info) color = LOG_COLORS.info;
    else if (debug) color = LOG_COLORS.debug;
    else if (str) color = LOG_COLORS.string;
    else if (path) color = LOG_COLORS.path;
    else if (kv) color = LOG_COLORS.key;
    else if (num) color = LOG_COLORS.number;

    parts.push(<span key={key++} style={{ color, fontWeight }}>{fullMatch}</span>);
    lastIdx = match.index + fullMatch!.length;
  }

  // Remaining text
  if (lastIdx < line.length) {
    const rest = line.slice(lastIdx);
    parts.push(<span key={key++} style={isError ? { color: "#ffa198" } : isWarn ? { color: "#e3b341", opacity: 0.85 } : undefined}>{rest}</span>);
  }

  return parts;
}
