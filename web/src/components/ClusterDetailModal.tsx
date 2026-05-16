import { useState, useEffect, useMemo } from "react";
import { createPortal } from "react-dom";
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import {
  AlertTriangle,
  Copy,
  Loader2,
  Maximize2,
  X,
} from "lucide-react";

import { monitoringApi } from "@/api/endpoints";
import type { AksAgentPool, WarmupDbInfo, WarmupStatus } from "@/api/endpoints";
import { ClusterModalKubectl } from "@/components/ClusterDiagnostics";
import { WarmupSection } from "@/components/WarmupSection";

// ---------------------------------------------------------------------------
// Helpers — shared with ClusterDiagnostics; kept duplicated here on purpose
// so the card body can render a compact summary without importing the whole
// diagnostics module (and pulling its tree-shake exclusions).
// ---------------------------------------------------------------------------

const SYSTEM_POOL_HINTS = ["systempool", "system", "agentpool"];

function isSystemPool(pool: string | undefined): boolean {
  if (!pool) return false;
  const p = pool.toLowerCase();
  return SYSTEM_POOL_HINTS.some((h) => p === h || p.startsWith(h));
}

function fmtCores(milli: number): string {
  if (milli <= 0) return "0";
  if (milli < 10_000) return (milli / 1000).toFixed(2);
  return (milli / 1000).toFixed(1);
}

function fmtGiB(ki: number): string {
  if (ki <= 0) return "0";
  const gib = ki / 1024 / 1024;
  if (gib >= 100) return gib.toFixed(0);
  if (gib >= 10) return gib.toFixed(1);
  return gib.toFixed(2);
}

export function ClusterDetails({
  clusterName,
  powerState,
  isTransitioning,
  agentPools,
  fqdn,
  networkPlugin,
  subscriptionId,
  resourceGroup,
  warmupDbs,
  warmupQuery,
  storageAccount,
  storageResourceGroup,
  acrResourceGroup,
  acrName,
  region,
  nodeSku,
  nodeCount,
  terminalResourceGroup,
  terminalVmName,
  kubeletObjectId,
}: {
  clusterName: string;
  powerState: string | null;
  isTransitioning: boolean;
  agentPools?: AksAgentPool[];
  fqdn?: string | null;
  networkPlugin?: string | null;
  subscriptionId: string;
  resourceGroup: string;
  warmupDbs?: WarmupDbInfo[];
  warmupQuery?: UseQueryResult<WarmupStatus>;
  storageAccount?: string;
  storageResourceGroup?: string;
  acrResourceGroup?: string;
  acrName?: string;
  region?: string;
  nodeSku?: string | null;
  nodeCount?: number | null;
  terminalResourceGroup?: string;
  terminalVmName?: string;
  kubeletObjectId?: string | null;
}) {
  const isRunning = powerState === "Running" && !isTransitioning;
  const [showModal, setShowModal] = useState(false);

  // Fast K8s metrics API — direct access (~1-3s instead of ~30s)
  const topQuery = useQuery({
    queryKey: ["aks-top-fast", subscriptionId, resourceGroup, clusterName],
    queryFn: () => monitoringApi.k8sTopNodes(subscriptionId, resourceGroup, clusterName),
    enabled: isRunning,
    staleTime: 30_000,
    retry: 1,
    refetchInterval: isRunning ? 60_000 : false,
  });

  // Aggregate the per-node metrics into the compact card-body summary.
  // The full per-node breakdown lives in the modal's Cluster Diagnostics
  // section so we don't render the same rows twice on the dashboard.
  const summary = useMemo(() => {
    const nodes = topQuery.data?.nodes ?? [];
    let cpuUsedM = 0;
    let cpuTotalM = 0;
    let memUsedKi = 0;
    let memTotalKi = 0;
    let systemCount = 0;
    let userCount = 0;
    let notReady = 0;
    let hot = 0;
    const pressureFlags = new Set<string>();
    for (const n of nodes) {
      cpuUsedM += n.cpu_m ?? 0;
      cpuTotalM += n.cpu_capacity_m ?? 0;
      memUsedKi += n.mem_ki ?? 0;
      memTotalKi += n.mem_capacity_ki ?? 0;
      if (isSystemPool(n.pool)) systemCount += 1;
      else userCount += 1;
      if (n.ready === false) notReady += 1;
      if (n.cpu_pct > 80 || n.memory_pct > 80) hot += 1;
      const conds = n.conditions ?? {};
      if (conds.MemoryPressure === "True") pressureFlags.add("MemoryPressure");
      if (conds.DiskPressure === "True") pressureFlags.add("DiskPressure");
      if (conds.PIDPressure === "True") pressureFlags.add("PIDPressure");
    }
    const cpuPct =
      cpuTotalM > 0 ? Math.round((cpuUsedM / cpuTotalM) * 1000) / 10 : 0;
    const memPct =
      memTotalKi > 0 ? Math.round((memUsedKi / memTotalKi) * 1000) / 10 : 0;
    return {
      total: nodes.length,
      systemCount,
      userCount,
      cpuUsedM,
      cpuTotalM,
      memUsedKi,
      memTotalKi,
      cpuPct,
      memPct,
      notReady,
      hot,
      pressure: [...pressureFlags],
    };
  }, [topQuery.data]);

  // ESC + body scroll lock for modal
  useEffect(() => {
    if (!showModal) return;
    const handleEsc = (e: KeyboardEvent) => {
      if (e.key === "Escape") setShowModal(false);
    };
    window.addEventListener("keydown", handleEsc);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", handleEsc);
      document.body.style.overflow = prev;
    };
  }, [showModal]);

  const healthy =
    summary.total > 0 &&
    summary.notReady === 0 &&
    summary.pressure.length === 0;

  return (
    <div style={{ marginTop: "var(--space-2)" }}>
      {/* ── Compact node-resources summary strip ──
          One-line aggregate: pool dots, totals, health. The per-node table
          lives in the modal's Cluster Diagnostics section so we don't repeat
          the same data twice on the dashboard.
          #3 — Rendered as a <button> so the click-to-expand affordance is
          discoverable (cursor + keyboard + visible Maximize2 icon). */}
      {isRunning && summary.total > 0 && (
        <button
          type="button"
          onClick={() => setShowModal(true)}
          aria-label="Open per-node breakdown"
          title="Open per-node breakdown"
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            padding: "8px 10px",
            borderRadius: 6,
            border: "1px solid var(--border-weak)",
            background: "var(--bg-secondary)",
            fontSize: 11,
            flexWrap: "wrap",
            cursor: "pointer",
            width: "100%",
            textAlign: "left",
            color: "inherit",
            font: "inherit",
          }}
        >
          {/* Pool dots */}
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            {summary.systemCount > 0 && (
              <span
                style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
                title={`${summary.systemCount} system node${summary.systemCount === 1 ? "" : "s"}`}
              >
                <span
                  style={{
                    width: 8,
                    height: 8,
                    borderRadius: 2,
                    background: "var(--warning)",
                  }}
                />
                <span
                  className="muted"
                  style={{ fontSize: 10, fontFamily: "var(--font-mono)" }}
                >
                  {summary.systemCount}
                </span>
              </span>
            )}
            {summary.userCount > 0 && (
              <span
                style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
                title={`${summary.userCount} user node${summary.userCount === 1 ? "" : "s"}`}
              >
                <span
                  style={{
                    width: 8,
                    height: 8,
                    borderRadius: 2,
                    background: "var(--accent)",
                  }}
                />
                <span
                  className="muted"
                  style={{ fontSize: 10, fontFamily: "var(--font-mono)" }}
                >
                  {summary.userCount}
                </span>
              </span>
            )}
            <span
              className="muted"
              style={{
                fontSize: 9,
                textTransform: "uppercase",
                letterSpacing: "0.06em",
              }}
            >
              {summary.total} {summary.total === 1 ? "node" : "nodes"}
            </span>
          </span>
          <span className="muted" style={{ fontSize: 11 }}>·</span>
          {/* CPU aggregate */}
          <span
            style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
            title={`${summary.cpuUsedM}m of ${summary.cpuTotalM}m`}
          >
            <span
              className="muted"
              style={{
                fontSize: 9,
                textTransform: "uppercase",
                letterSpacing: "0.06em",
              }}
            >
              CPU
            </span>
            <span style={{ fontFamily: "var(--font-mono)", fontSize: 11 }}>
              <span style={{ color: "var(--text-primary)" }}>
                {fmtCores(summary.cpuUsedM)}
              </span>
              <span className="muted"> / {fmtCores(summary.cpuTotalM)} cores</span>{" "}
              <span className="muted">({summary.cpuPct}%)</span>
            </span>
          </span>
          <span className="muted" style={{ fontSize: 11 }}>·</span>
          {/* Memory aggregate */}
          <span
            style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
            title={`${Math.round(summary.memUsedKi / 1024)}Mi of ${Math.round(summary.memTotalKi / 1024)}Mi`}
          >
            <span
              className="muted"
              style={{
                fontSize: 9,
                textTransform: "uppercase",
                letterSpacing: "0.06em",
              }}
            >
              MEM
            </span>
            <span style={{ fontFamily: "var(--font-mono)", fontSize: 11 }}>
              <span style={{ color: "var(--text-primary)" }}>
                {fmtGiB(summary.memUsedKi)}
              </span>
              <span className="muted"> / {fmtGiB(summary.memTotalKi)} GiB</span>{" "}
              <span className="muted">({summary.memPct}%)</span>
            </span>
          </span>
          {/* Health flag — pushed right */}
          <span style={{ marginLeft: "auto", display: "inline-flex", gap: 6 }}>
            {healthy && (
              <span
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 4,
                  fontSize: 10,
                  color: "var(--success)",
                  fontWeight: 500,
                }}
              >
                <span
                  style={{
                    width: 7,
                    height: 7,
                    borderRadius: "50%",
                    background: "var(--success)",
                  }}
                />
                all Ready
              </span>
            )}
            {summary.notReady > 0 && (
              <span
                className="dv3-pill dv3-pill-danger"
                style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
              >
                <AlertTriangle size={10} strokeWidth={1.75} />
                {summary.notReady} NotReady
              </span>
            )}
            {summary.pressure.length > 0 && (
              <span
                className="dv3-pill dv3-pill-warning"
                title={summary.pressure.join(", ")}
              >
                {summary.pressure.length === 1
                  ? summary.pressure[0]
                  : `${summary.pressure.length} pressure`}
              </span>
            )}
            {summary.notReady === 0 &&
              summary.pressure.length === 0 &&
              summary.hot > 0 && (
                <span
                  className="dv3-pill dv3-pill-warning"
                  title="One or more nodes above 80% CPU/memory"
                >
                  {summary.hot} hot
                </span>
              )}
            {topQuery.isFetching && (
              <Loader2
                size={10}
                className="spin"
                style={{ color: "var(--text-faint)" }}
              />
            )}
            {/* #3 — explicit modal-open affordance so users can tell the
                whole strip is clickable. Sits to the right of the health
                pill, opacity 0.6 when idle for low chrome. */}
            <span
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 3,
                fontSize: 10,
                color: "var(--accent)",
                opacity: 0.7,
                paddingLeft: 6,
                borderLeft: "1px solid var(--border-weak)",
              }}
            >
              <Maximize2 size={11} strokeWidth={1.75} />
              <span
                style={{
                  textTransform: "uppercase",
                  letterSpacing: "0.06em",
                  fontSize: 9,
                }}
              >
                Details
              </span>
            </span>
          </span>
        </button>
      )}

      {isRunning && topQuery.isLoading && summary.total === 0 && (
        <div
          className="muted"
          style={{
            fontSize: 10,
            marginTop: 4,
            display: "flex",
            alignItems: "center",
            gap: 4,
          }}
        >
          <Loader2 size={10} className="spin" /> Loading node metrics...
        </div>
      )}

      {!isRunning && (
        <div className="muted" style={{ fontSize: 10, marginTop: 4 }}>
          Start the cluster to view node metrics.
        </div>
      )}

      {/* Full details modal */}
      {showModal &&
        createPortal(
          <div
            className="glass-dialog-backdrop"
            onClick={(e) => {
              if (e.target === e.currentTarget) setShowModal(false);
            }}
            role="dialog"
            aria-modal="true"
            aria-label={`${clusterName} Details`}
          >
            <div
              className="glass-card glass-card--strong glass-dialog"
              onClick={(e) => e.stopPropagation()}
              style={{
                maxWidth: 1180,
                width: "calc(100vw - 48px)",
                maxHeight: "92vh",
                display: "flex",
                flexDirection: "column",
                padding: 0,
                overflow: "hidden",
              }}
            >
              {/* ── Premium header with accent gradient ── */}
              <div
                style={{
                  padding: "20px 24px 16px",
                  background:
                    "linear-gradient(135deg, rgba(110,159,255,0.08) 0%, rgba(184,119,217,0.06) 100%)",
                  borderBottom: "1px solid var(--border-weak)",
                }}
              >
                <div
                  style={{
                    display: "flex",
                    alignItems: "flex-start",
                    justifyContent: "space-between",
                  }}
                >
                  <div>
                    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                      <div
                        style={{
                          width: 36,
                          height: 36,
                          borderRadius: 10,
                          background:
                            "linear-gradient(135deg, var(--accent), var(--purple))",
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "center",
                          boxShadow: "0 4px 12px rgba(110,159,255,0.25)",
                        }}
                      >
                        <span style={{ fontSize: 16 }}>⎈</span>
                      </div>
                      <div>
                        <h3
                          style={{
                            margin: 0,
                            fontSize: 18,
                            fontWeight: 700,
                            letterSpacing: "-0.02em",
                          }}
                        >
                          {clusterName}
                        </h3>
                        <div
                          style={{
                            display: "flex",
                            alignItems: "center",
                            gap: 8,
                            marginTop: 2,
                          }}
                        >
                          <span
                            style={{
                              display: "inline-flex",
                              alignItems: "center",
                              gap: 4,
                              fontSize: 11,
                              fontWeight: 600,
                              color:
                                powerState === "Running"
                                  ? "var(--success)"
                                  : "var(--warning)",
                            }}
                          >
                            <span
                              style={{
                                width: 6,
                                height: 6,
                                borderRadius: "50%",
                                background:
                                  powerState === "Running"
                                    ? "var(--success)"
                                    : "var(--warning)",
                                boxShadow:
                                  powerState === "Running"
                                    ? "0 0 8px var(--success)"
                                    : "none",
                                animation:
                                  powerState === "Running"
                                    ? "blink 1.8s ease-in-out infinite"
                                    : "none",
                              }}
                            />
                            {powerState ?? "Unknown"}
                          </span>
                          {fqdn && (
                            <span className="muted" style={{ fontSize: 10 }}>
                              ·
                            </span>
                          )}
                          {fqdn && (
                            <code
                              style={{
                                fontSize: 9,
                                color: "var(--text-faint)",
                                background: "rgba(255,255,255,0.04)",
                                padding: "2px 6px",
                                borderRadius: 4,
                              }}
                            >
                              {fqdn}
                            </code>
                          )}
                        </div>
                      </div>
                    </div>
                  </div>
                  <button
                    className="glass-button"
                    onClick={() => setShowModal(false)}
                    style={{
                      padding: "6px 8px",
                      border: "none",
                      background: "rgba(255,255,255,0.05)",
                    }}
                    title="Close (Esc)"
                  >
                    <X size={16} strokeWidth={1.5} />
                  </button>
                </div>

                {/* ── Stat cards row ── */}
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "repeat(auto-fit, minmax(100px, 1fr))",
                    gap: 10,
                    marginTop: 16,
                  }}
                >
                  {[
                    {
                      label: "Nodes",
                      value: agentPools?.[0]?.count ?? "—",
                      sub: agentPools?.[0]?.vm_size ?? "",
                    },
                    { label: "K8s", value: networkPlugin ?? "—", sub: "network" },
                    {
                      label: "Pools",
                      value: String(agentPools?.length ?? 0),
                      sub: agentPools?.map((p) => p.name).join(", ") ?? "",
                    },
                    {
                      label: "OS",
                      value: agentPools?.[0]?.os_type ?? "—",
                      sub: agentPools?.[0]?.mode ?? "",
                    },
                  ].map((s) => (
                    <div
                      key={s.label}
                      style={{
                        padding: "10px 12px",
                        borderRadius: 8,
                        background: "rgba(255,255,255,0.03)",
                        border: "1px solid var(--border-weak)",
                      }}
                    >
                      <div
                        className="muted"
                        style={{
                          fontSize: 9,
                          textTransform: "uppercase",
                          letterSpacing: "0.06em",
                        }}
                      >
                        {s.label}
                      </div>
                      <div
                        style={{
                          fontSize: 16,
                          fontWeight: 700,
                          marginTop: 2,
                          letterSpacing: "-0.02em",
                        }}
                      >
                        {s.value}
                      </div>
                      <div
                        className="muted"
                        style={{
                          fontSize: 9,
                          marginTop: 1,
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {s.sub}
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              {/* ── Scrollable body ── */}
              <div style={{ overflowY: "auto", flex: 1, padding: "16px 24px 24px" }}>
                {/* ── Identity ── */}
                {kubeletObjectId && (
                  <div style={{ marginBottom: 20 }}>
                    <div
                      style={{
                        fontSize: 11,
                        fontWeight: 600,
                        marginBottom: 8,
                        display: "flex",
                        alignItems: "center",
                        gap: 6,
                      }}
                    >
                      <span
                        style={{
                          width: 3,
                          height: 14,
                          borderRadius: 2,
                          background: "var(--purple)",
                        }}
                      />
                      Identity
                    </div>
                    <div
                      style={{
                        borderRadius: 8,
                        border: "1px solid var(--border-weak)",
                        padding: "10px 12px",
                        display: "flex",
                        alignItems: "center",
                        gap: 10,
                        flexWrap: "wrap",
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
                        Kubelet OID
                      </span>
                      <code
                        style={{
                          fontSize: 11,
                          fontFamily: "var(--font-mono)",
                          color: "var(--text-primary)",
                          wordBreak: "break-all",
                        }}
                      >
                        {kubeletObjectId}
                      </code>
                      <button
                        className="glass-button"
                        style={{ padding: "2px 8px", fontSize: 10 }}
                        onClick={() => navigator.clipboard.writeText(kubeletObjectId)}
                        title="Copy OID"
                      >
                        <Copy size={11} strokeWidth={1.5} /> Copy
                      </button>
                      <span
                        className="muted"
                        style={{
                          fontSize: 10,
                          marginLeft: "auto",
                        }}
                      >
                        AcrPull on the registry must be granted to this object id.
                      </span>
                    </div>
                  </div>
                )}
                {/* ── Node Pools table ── */}
                {agentPools && agentPools.length > 0 && (
                  <div style={{ marginBottom: 20 }}>
                    <div
                      style={{
                        fontSize: 11,
                        fontWeight: 600,
                        marginBottom: 8,
                        display: "flex",
                        alignItems: "center",
                        gap: 6,
                      }}
                    >
                      <span
                        style={{
                          width: 3,
                          height: 14,
                          borderRadius: 2,
                          background: "var(--accent)",
                        }}
                      />
                      Node Pools
                    </div>
                    <div
                      style={{
                        borderRadius: 8,
                        border: "1px solid var(--border-weak)",
                        overflow: "hidden",
                      }}
                    >
                      <table
                        style={{
                          width: "100%",
                          fontSize: 11,
                          borderCollapse: "collapse",
                        }}
                      >
                        <thead>
                          <tr style={{ background: "var(--bg-tertiary)" }}>
                            {[
                              "Pool",
                              "SKU",
                              "Nodes",
                              "OS",
                              "Mode",
                              "Autoscale",
                              "State",
                            ].map((h) => (
                              <th
                                key={h}
                                style={{
                                  textAlign: h === "Nodes" ? "center" : "left",
                                  padding: "8px 10px",
                                  color: "var(--text-faint)",
                                  fontSize: 9,
                                  textTransform: "uppercase",
                                  letterSpacing: "0.05em",
                                  fontWeight: 500,
                                }}
                              >
                                {h}
                              </th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {agentPools.map((p, i) => (
                            <tr
                              key={p.name}
                              style={{
                                background:
                                  i % 2 === 0 ? "transparent" : "rgba(255,255,255,0.015)",
                                borderTop: "1px solid var(--border-weak)",
                              }}
                            >
                              <td style={{ padding: "8px 10px", fontWeight: 600 }}>
                                {p.name}
                              </td>
                              <td style={{ padding: "8px 10px" }}>
                                <code style={{ fontSize: 10 }}>{p.vm_size}</code>
                              </td>
                              <td
                                style={{
                                  padding: "8px 10px",
                                  textAlign: "center",
                                  fontWeight: 600,
                                }}
                              >
                                {p.count}
                              </td>
                              <td style={{ padding: "8px 10px" }}>{p.os_type}</td>
                              <td style={{ padding: "8px 10px" }}>
                                <span
                                  style={{
                                    fontSize: 9,
                                    padding: "2px 6px",
                                    borderRadius: 4,
                                    background:
                                      p.mode === "System"
                                        ? "rgba(110,159,255,0.1)"
                                        : "rgba(115,191,105,0.1)",
                                    color:
                                      p.mode === "System"
                                        ? "var(--accent)"
                                        : "var(--success)",
                                  }}
                                >
                                  {p.mode}
                                </span>
                              </td>
                              <td style={{ padding: "8px 10px", fontSize: 10 }}>
                                {p.enable_auto_scaling ? (
                                  <span style={{ color: "var(--success)" }}>
                                    {p.min_count}–{p.max_count}
                                  </span>
                                ) : (
                                  <span className="muted">Off</span>
                                )}
                              </td>
                              <td style={{ padding: "8px 10px" }}>
                                <span
                                  style={{
                                    display: "inline-flex",
                                    alignItems: "center",
                                    gap: 4,
                                    fontSize: 10,
                                    fontWeight: 500,
                                    color:
                                      p.power_state === "Running"
                                        ? "var(--success)"
                                        : "var(--warning)",
                                  }}
                                >
                                  <span
                                    style={{
                                      width: 5,
                                      height: 5,
                                      borderRadius: "50%",
                                      background:
                                        p.power_state === "Running"
                                          ? "var(--success)"
                                          : "var(--warning)",
                                    }}
                                  />
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

                {/* kubectl sections — only when fully running */}
                {!isRunning && (
                  <div
                    style={{
                      padding: "24px 20px",
                      borderRadius: 8,
                      textAlign: "center",
                      background: "rgba(255,255,255,0.02)",
                      border: "1px dashed var(--border-weak)",
                      color: "var(--text-faint)",
                      fontSize: 12,
                      display: "flex",
                      flexDirection: "column",
                      alignItems: "center",
                      gap: 8,
                    }}
                  >
                    {isTransitioning ||
                    (powerState !== "Stopped" && powerState !== "Running") ? (
                      <>
                        <Loader2
                          size={18}
                          className="spin"
                          style={{ color: "var(--accent)" }}
                        />
                        <span>
                          Cluster is <strong>{powerState ?? "transitioning"}</strong> —
                          diagnostics will be available once it finishes starting.
                        </span>
                      </>
                    ) : (
                      <span>
                        Start the cluster to view diagnostics and run kubectl commands.
                      </span>
                    )}
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

                {/* Warmup section — DB cache management */}
                {isRunning && (
                  <WarmupSection
                    subscriptionId={subscriptionId}
                    resourceGroup={resourceGroup}
                    clusterName={clusterName}
                    warmupDbs={warmupDbs}
                    warmupQuery={warmupQuery}
                    storageAccount={storageAccount}
                    storageResourceGroup={storageResourceGroup}
                    acrResourceGroup={acrResourceGroup}
                    acrName={acrName}
                    region={region}
                    nodeSku={nodeSku}
                    nodeCount={nodeCount}
                    terminalResourceGroup={terminalResourceGroup}
                    terminalVmName={terminalVmName}
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
