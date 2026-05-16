import { useState, useEffect } from "react";
import { createPortal } from "react-dom";
import { useQuery } from "@tanstack/react-query";
import { Loader2, Plus, AlertTriangle, CheckCircle2, X } from "lucide-react";

import { aksApi, monitoringApi } from "@/api/endpoints";
import { formatApiError } from "@/api/client";
import { MonitorCard } from "@/components/MonitorCard";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { ClusterItem } from "@/components/ClusterItem";
import {
  DEFAULT_AKS_SKU,
  DEFAULT_AKS_SYSTEM_SKU,
  describeAksSku,
  formatAksSkuOption,
  groupAksSkus,
  useAksSkus,
} from "@/hooks/useAksSkus";
import { useAutoRefreshInterval } from "@/hooks/useAutoRefresh";

const DEFAULT_NODE_COUNT = 10;
const DEFAULT_SYSTEM_NODE_COUNT = 1;
const MAX_SYSTEM_NODE_COUNT = 3;

const CLUSTER_NAME_RE = /^[a-zA-Z][a-zA-Z0-9-]{1,62}$/;

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

  // Provision form state
  const [showProvision, setShowProvision] = useState(false);
  const [clusterName, setClusterName] = useState("elb-cluster");
  const [nodeSku, setNodeSku] = useState(DEFAULT_AKS_SKU);
  const [nodeCount, setNodeCount] = useState(DEFAULT_NODE_COUNT);
  const [systemVmSize, setSystemVmSize] = useState(DEFAULT_AKS_SYSTEM_SKU);
  const [systemNodeCount, setSystemNodeCount] = useState(DEFAULT_SYSTEM_NODE_COUNT);
  const [provStatus, setProvStatus] = useState<"idle" | "creating" | "done" | "error">(
    "idle",
  );
  const [provError, setProvError] = useState<string | null>(null);
  const [provStart, setProvStart] = useState<number | null>(null);
  const [elapsed, setElapsed] = useState(0);

  // Role assignment result (shown after provision completes)
  const [roleResult] = useState<string[] | null>(null);

  // Start/Stop/Delete loading
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  // Track clusters in transition (starting/stopping) until actual state changes
  const [transitioning, setTransitioning] = useState<
    Map<string, "starting" | "stopping">
  >(new Map());

  const {
    skus: skuOptions,
    defaultSystemSku,
    groupLabels,
    groupOrder,
  } = useAksSkus({ enabled });

  // Adopt the backend's system-pool default the first time it loads. The
  // bundled fallback uses the same value so this is a no-op offline.
  useEffect(() => {
    if (defaultSystemSku && systemVmSize === DEFAULT_AKS_SYSTEM_SKU) {
      setSystemVmSize(defaultSystemSku);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [defaultSystemSku]);

  useEffect(() => {
    if (provStatus !== "creating") return;
    const timer = setInterval(
      () => setElapsed(Math.floor((Date.now() - (provStart ?? Date.now())) / 1000)),
      1000,
    );
    return () => clearInterval(timer);
  }, [provStatus, provStart]);

  // ESC to close provision modal
  useEffect(() => {
    if (!showProvision) return;
    const handleEsc = (e: KeyboardEvent) => {
      if (e.key === "Escape") setShowProvision(false);
    };
    window.addEventListener("keydown", handleEsc);
    return () => window.removeEventListener("keydown", handleEsc);
  }, [showProvision]);

  const handleProvision = async () => {
    if (!region) return;
    setProvStatus("creating");
    setProvError(null);
    setProvStart(Date.now());
    setShowProvision(false); // Close modal immediately
    try {
      await aksApi.provision({
        subscription_id: subscriptionId,
        resource_group: resourceGroup,
        region,
        cluster_name: clusterName,
        node_sku: nodeSku,
        node_count: nodeCount,
        // Sibling repo's two-pool layout (constants.py):
        //   systempool (mode=System, CriticalAddonsOnly taint)
        //   blastpool  (mode=User, workload=blast taint)
        system_vm_size: systemVmSize,
        system_node_count: systemNodeCount,
        acr_resource_group: acrResourceGroup || "",
        acr_name: acrName || "",
        storage_resource_group: storageResourceGroup || resourceGroup,
        storage_account: storageAccount || "",
      });
      // Orchestrator started — stay in "creating" state until cluster appears
    } catch (e) {
      setProvError(formatApiError(e, "aks"));
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
      setActionError(`Delete failed: ${formatApiError(e, "aks")}`);
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
      setTransitioning((prev) =>
        new Map(prev).set(name, action === "start" ? "starting" : "stopping"),
      );
    } catch (e) {
      setActionError(`${action} failed: ${formatApiError(e, "aks")}`);
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
      if (!cluster) {
        next.delete(name);
        changed = true;
        continue;
      }
      const reached =
        expected === "starting"
          ? cluster.power_state === "Running"
          : cluster.power_state === "Stopped";
      if (reached) {
        next.delete(name);
        changed = true;
      }
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

  // While creating, poll AKS list faster (every 10s) to detect new cluster
  useEffect(() => {
    if (provStatus !== "creating") return;
    const t = setInterval(() => query.refetch(), 10_000);
    return () => clearInterval(t);
  }, [provStatus, query]);

  // Detect when provisioning cluster appears in the list
  useEffect(() => {
    if (provStatus !== "creating" || !query.data?.clusters) return;
    const found = query.data.clusters.find((c) => c.name === clusterName);
    if (found) {
      setProvStatus("done");
    }
  }, [provStatus, query.data, clusterName]);

  const clusterNameValid = CLUSTER_NAME_RE.test(clusterName);
  const selectedSku = skuOptions.find((option) => option.name === nodeSku);
  const selectedSystemSku = skuOptions.find(
    (option) => option.name === systemVmSize,
  );
  const blastCost = (selectedSku?.hourlyUsd ?? 1.34) * nodeCount;
  const systemCost = (selectedSystemSku?.hourlyUsd ?? 0.096) * systemNodeCount;
  const estimatedCost = blastCost + systemCost;

  // Pre-compute the two pool dropdowns so the JSX stays declarative.
  const blastGroups = groupAksSkus(skuOptions, "blast", groupOrder, groupLabels);
  const systemGroups = groupAksSkus(skuOptions, "system", groupOrder, groupLabels);

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
      title="Azure Kubernetes Service Cluster"
      subtitle={enabled ? resourceGroup : "Configure subscription / RG"}
      status={provStatus === "creating" ? "loading" : status}
      fetching={query.isFetching}
      lastRefreshed={query.dataUpdatedAt ? new Date(query.dataUpdatedAt) : null}
      onRefresh={() => {
        setActionError(null);
        setProvError(null);
        query.refetch();
      }}
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

      {/* Loading skeleton */}
      {enabled && query.isLoading && (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {[1, 2].map((i) => (
            <div key={i} className="glass-card" style={{ padding: "var(--space-3)" }}>
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                }}
              >
                <div
                  style={{
                    width: 140,
                    height: 14,
                    background: "var(--glass-bg-strong)",
                    borderRadius: 4,
                  }}
                />
                <div style={{ display: "flex", gap: 8 }}>
                  <div
                    style={{
                      width: 80,
                      height: 12,
                      background: "var(--glass-bg-strong)",
                      borderRadius: 4,
                    }}
                  />
                  <div
                    style={{
                      width: 50,
                      height: 22,
                      background: "var(--glass-bg-strong)",
                      borderRadius: 4,
                    }}
                  />
                </div>
              </div>
              <div style={{ marginTop: 8, display: "flex", gap: 8 }}>
                <div
                  style={{
                    width: 200,
                    height: 11,
                    background: "var(--glass-bg)",
                    borderRadius: 3,
                  }}
                />
              </div>
              <div style={{ marginTop: 8, display: "flex", gap: 8 }}>
                <div
                  style={{
                    width: 120,
                    height: 10,
                    background: "var(--glass-bg)",
                    borderRadius: 3,
                  }}
                />
              </div>
            </div>
          ))}
        </div>
      )}

      {query.data?.clusters.length === 0 &&
        provStatus !== "creating" &&
        provStatus !== "done" && (
          <div className="muted">
            No AKS clusters found. Click "+ Add Cluster" below to provision one.
          </div>
        )}

      {/* Provision modal */}
      {showProvision &&
        createPortal(
          <div
            style={{
              position: "fixed",
              inset: 0,
              background: "rgba(0,0,0,0.6)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              zIndex: 200,
              backdropFilter: "blur(4px)",
            }}
            onClick={(e) => {
              if (e.target === e.currentTarget) setShowProvision(false);
            }}
          >
            <div
              style={{
                background: "var(--bg-primary)",
                border: "1px solid var(--border-medium)",
                borderRadius: 16,
                boxShadow: "0 8px 48px rgba(0,0,0,0.5)",
                width: "min(760px, calc(100vw - 32px))",
                maxHeight: "90vh",
                overflow: "auto",
              }}
            >
              <div
                style={{
                  padding: "20px 24px 0",
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                }}
              >
                <h2 style={{ fontSize: 16, fontWeight: 700, margin: 0 }}>
                  Create AKS Cluster
                </h2>
                <button
                  onClick={() => setShowProvision(false)}
                  style={{
                    background: "none",
                    border: "none",
                    color: "var(--text-faint)",
                    cursor: "pointer",
                    padding: 4,
                  }}
                  title="Close"
                >
                  <X size={18} />
                </button>
              </div>
              <div style={{ padding: "16px 24px 24px", display: "grid", gap: 16 }}>
                <div>
                  <label
                    style={{
                      fontSize: 11,
                      color: "var(--text-muted)",
                      display: "block",
                      marginBottom: 4,
                    }}
                  >
                    Cluster Name
                  </label>
                  <input
                    type="text"
                    value={clusterName}
                    onChange={(e) => setClusterName(e.target.value)}
                    className="glass-input"
                    style={{ width: "100%", fontSize: 13 }}
                    placeholder="elb-cluster"
                    autoFocus
                  />
                  {!clusterNameValid && clusterName.length > 0 && (
                    <div style={{ fontSize: 10, color: "var(--danger)", marginTop: 4 }}>
                      Must start with a letter, contain only letters/digits/hyphens, 2–63
                      chars.
                    </div>
                  )}
                </div>

                <div>
                  <div
                    style={{
                      fontSize: 11,
                      fontWeight: 600,
                      color: "var(--text-primary)",
                      marginBottom: 8,
                      textTransform: "uppercase",
                      letterSpacing: 0.5,
                    }}
                  >
                    Workload pool ·{" "}
                    <span style={{ color: "var(--text-muted)", fontWeight: 400 }}>
                      blastpool
                    </span>
                  </div>
                  <div
                    style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}
                  >
                    <div>
                      <label
                        style={{
                          fontSize: 11,
                          color: "var(--text-muted)",
                          display: "block",
                          marginBottom: 4,
                        }}
                      >
                        Node SKU
                      </label>
                      <select
                        value={nodeSku}
                        onChange={(e) => setNodeSku(e.target.value)}
                        className="glass-input"
                        style={{ width: "100%", fontSize: 13 }}
                      >
                        {blastGroups.map((group) => (
                          <optgroup key={group.id} label={`── ${group.label} ──`}>
                            {group.skus.map((option) => (
                              <option key={option.name} value={option.name}>
                                {formatAksSkuOption(option)}
                              </option>
                            ))}
                          </optgroup>
                        ))}
                      </select>
                      <div className="muted" style={{ fontSize: 10, marginTop: 3 }}>
                        {describeAksSku(selectedSku)}
                      </div>
                    </div>
                    <div>
                      <label
                        style={{
                          fontSize: 11,
                          color: "var(--text-muted)",
                          display: "block",
                          marginBottom: 4,
                        }}
                      >
                        Node Count
                      </label>
                      <input
                        type="number"
                        min={1}
                        max={100}
                        value={nodeCount}
                        onChange={(e) =>
                          setNodeCount(
                            Math.max(1, Math.min(100, parseInt(e.target.value) || 1)),
                          )
                        }
                        className="glass-input"
                        style={{ width: "100%", fontSize: 13 }}
                      />
                    </div>
                  </div>
                </div>

                <div>
                  <div
                    style={{
                      fontSize: 11,
                      fontWeight: 600,
                      color: "var(--text-primary)",
                      marginBottom: 8,
                      textTransform: "uppercase",
                      letterSpacing: 0.5,
                    }}
                  >
                    System pool ·{" "}
                    <span style={{ color: "var(--text-muted)", fontWeight: 400 }}>
                      systempool · CriticalAddonsOnly
                    </span>
                  </div>
                  <div
                    style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}
                  >
                    <div>
                      <label
                        style={{
                          fontSize: 11,
                          color: "var(--text-muted)",
                          display: "block",
                          marginBottom: 4,
                        }}
                      >
                        System VM size
                      </label>
                      <select
                        value={systemVmSize}
                        onChange={(e) => setSystemVmSize(e.target.value)}
                        className="glass-input"
                        style={{ width: "100%", fontSize: 13 }}
                      >
                        {systemGroups.map((group) => (
                          <optgroup key={group.id} label={`── ${group.label} ──`}>
                            {group.skus.map((option) => (
                              <option key={option.name} value={option.name}>
                                {formatAksSkuOption(option)}
                              </option>
                            ))}
                          </optgroup>
                        ))}
                      </select>
                      <div className="muted" style={{ fontSize: 10, marginTop: 3 }}>
                        Hosts CoreDNS / metrics-server / CSI · {describeAksSku(selectedSystemSku)}
                      </div>
                    </div>
                    <div>
                      <label
                        style={{
                          fontSize: 11,
                          color: "var(--text-muted)",
                          display: "block",
                          marginBottom: 4,
                        }}
                      >
                        System node count (1–{MAX_SYSTEM_NODE_COUNT})
                      </label>
                      <input
                        type="number"
                        min={1}
                        max={MAX_SYSTEM_NODE_COUNT}
                        value={systemNodeCount}
                        onChange={(e) =>
                          setSystemNodeCount(
                            Math.max(
                              1,
                              Math.min(
                                MAX_SYSTEM_NODE_COUNT,
                                parseInt(e.target.value) || 1,
                              ),
                            ),
                          )
                        }
                        className="glass-input"
                        style={{ width: "100%", fontSize: 13 }}
                      />
                    </div>
                  </div>
                </div>

                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
                  <div>
                    <label
                      style={{
                        fontSize: 11,
                        color: "var(--text-muted)",
                        display: "block",
                        marginBottom: 4,
                      }}
                    >
                      Region
                    </label>
                    <div
                      style={{
                        fontSize: 13,
                        color: "var(--text-primary)",
                        padding: "6px 0",
                      }}
                    >
                      {region || "Not set"}
                    </div>
                  </div>
                  <div>
                    <label
                      style={{
                        fontSize: 11,
                        color: "var(--text-muted)",
                        display: "block",
                        marginBottom: 4,
                      }}
                    >
                      Resource Group
                    </label>
                    <div
                      style={{
                        fontSize: 13,
                        color: "var(--text-primary)",
                        padding: "6px 0",
                      }}
                    >
                      {resourceGroup}
                    </div>
                  </div>
                </div>

                <div
                  style={{
                    padding: "10px 14px",
                    background: "var(--glass-bg)",
                    border: "1px solid var(--glass-border)",
                    borderRadius: 8,
                    fontSize: 12,
                    color: "var(--text-muted)",
                  }}
                >
                  Est. cost:{" "}
                  <strong style={{ color: "var(--text-primary)" }}>
                    ~${estimatedCost.toFixed(2)}/hr
                  </strong>
                  <div style={{ fontSize: 11, marginTop: 4 }}>
                    blastpool: {nodeCount} × {nodeSku} (~${blastCost.toFixed(2)}/hr) ·
                    systempool: {systemNodeCount} × {systemVmSize} (~$
                    {systemCost.toFixed(2)}/hr)
                  </div>
                  {!region && (
                    <span style={{ color: "var(--danger)", marginLeft: 8 }}>
                      Region required
                    </span>
                  )}
                </div>

                {provError && (
                  <div
                    style={{
                      fontSize: 12,
                      color: "var(--danger)",
                      display: "flex",
                      alignItems: "center",
                      gap: 6,
                    }}
                  >
                    <AlertTriangle size={12} /> {provError}
                  </div>
                )}

                <div style={{ display: "flex", gap: 10, justifyContent: "flex-end" }}>
                  <button
                    className="glass-button"
                    onClick={() => setShowProvision(false)}
                    style={{ fontSize: 12, padding: "8px 16px" }}
                  >
                    Cancel
                  </button>
                  <button
                    className="glass-button glass-button--primary"
                    onClick={handleProvision}
                    disabled={provStatus === "creating" || !region || !clusterNameValid}
                    style={{ fontSize: 12, padding: "8px 20px" }}
                  >
                    {provStatus === "creating" ? (
                      <>
                        <Loader2 size={12} className="spin" /> Creating...
                      </>
                    ) : (
                      <>
                        <Plus size={12} /> Create Cluster
                      </>
                    )}
                  </button>
                </div>
              </div>
            </div>
          </div>,
          document.body,
        )}

      {/* Provisioning banner — persistent until cluster appears */}
      {provStatus === "creating" && (
        <div
          className="glass-card"
          style={{
            padding: "12px 16px",
            marginBottom: "var(--space-3)",
            border: "1px solid rgba(110,159,255,0.25)",
            background: "rgba(110,159,255,0.04)",
          }}
        >
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <Loader2 size={16} className="spin" style={{ color: "var(--accent)" }} />
              <div>
                <div
                  style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)" }}
                >
                  {clusterName}
                </div>
                <div style={{ fontSize: 11, color: "var(--accent)" }}>
                  Provisioning... {formatTime(elapsed)}
                </div>
              </div>
            </div>
            <div style={{ textAlign: "right" }}>
              <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
                blastpool {nodeCount} × {nodeSku}
              </div>
              <div style={{ fontSize: 10, color: "var(--text-faint)" }}>
                systempool {systemNodeCount} × {systemVmSize} · Est. 5–10 min
              </div>
            </div>
          </div>
        </div>
      )}
      {provStatus === "done" && (
        <div
          style={{
            padding: "8px 12px",
            background: "rgba(106,214,163,0.06)",
            border: "1px solid rgba(106,214,163,0.2)",
            borderRadius: 8,
            fontSize: 12,
            color: "var(--success)",
            marginBottom: "var(--space-3)",
            display: "flex",
            alignItems: "center",
            gap: 6,
          }}
        >
          <CheckCircle2 size={14} /> Cluster <strong>{clusterName}</strong> is ready.
          {roleResult && roleResult.length > 0 && (
            <span className="muted" style={{ fontSize: 11 }}>
              {" "}
              · Roles: {roleResult.join(", ")}
            </span>
          )}
        </div>
      )}
      {provError && (
        <div
          style={{ fontSize: 12, color: "var(--danger)", marginBottom: "var(--space-3)" }}
        >
          <AlertTriangle size={12} style={{ verticalAlign: "middle" }} /> {provError}
        </div>
      )}

      {/* #6 — Compact "+ Add Cluster" pill above the list when clusters
          already exist; the big dashed CTA at the bottom only renders when
          the list is empty (see below). Right-aligned so it doesn't compete
          with the cluster headers underneath. */}
      {enabled && !query.isLoading && !noClusters && (
        <div
          style={{
            display: "flex",
            justifyContent: "flex-end",
            marginBottom: "var(--space-2)",
          }}
        >
          <button
            onClick={() => setShowProvision(true)}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
              padding: "3px 9px",
              background: "none",
              border: "1px solid var(--border-medium)",
              borderRadius: 999,
              color: "var(--text-muted)",
              fontSize: 11,
              cursor: "pointer",
              transition: "border-color 0.15s, color 0.15s",
            }}
            onMouseEnter={(e) => {
              e.currentTarget.style.borderColor = "var(--accent)";
              e.currentTarget.style.color = "var(--accent)";
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.borderColor = "var(--border-medium)";
              e.currentTarget.style.color = "var(--text-muted)";
            }}
          >
            <Plus size={12} strokeWidth={1.5} /> Add Cluster
          </button>
        </div>
      )}

      {/* Existing clusters */}
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
            transitioning={transitioning}
            actionLoading={actionLoading}
            onStartStop={handleStartStop}
            onDelete={setDeleteTarget}
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

      {/* #6 — The big dashed "Add Cluster" CTA only renders when the list
          is empty. When clusters exist, the compact pill above the list
          (rendered earlier) is the entry point. */}
      {enabled && !query.isLoading && noClusters && (
        <button
          onClick={() => setShowProvision(true)}
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: 6,
            width: "100%",
            marginTop: 8,
            padding: "8px 0",
            background: "none",
            border: "1px dashed var(--border-medium)",
            borderRadius: 8,
            color: "var(--text-muted)",
            fontSize: 12,
            cursor: "pointer",
            transition: "border-color 0.15s, color 0.15s",
          }}
          onMouseEnter={(e) => {
            e.currentTarget.style.borderColor = "var(--accent)";
            e.currentTarget.style.color = "var(--accent)";
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.borderColor = "var(--border-medium)";
            e.currentTarget.style.color = "var(--text-muted)";
          }}
        >
          <Plus size={14} strokeWidth={1.5} /> Provision your first cluster
        </button>
      )}

      {actionError && (
        <div
          style={{ marginTop: "var(--space-2)", fontSize: 11, color: "var(--danger)" }}
        >
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
