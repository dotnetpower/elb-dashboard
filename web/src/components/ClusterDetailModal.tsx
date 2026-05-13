import { useState, useEffect } from "react";
import { createPortal } from "react-dom";
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { Loader2, Maximize2, X } from "lucide-react";

import { monitoringApi } from "@/api/endpoints";
import type { AksAgentPool, WarmupDbInfo, WarmupStatus } from "@/api/endpoints";
import { ClusterModalKubectl } from "@/components/ClusterDiagnostics";
import { WarmupSection } from "@/components/WarmupSection";

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

  const nodeMetrics = (topQuery.data?.nodes ?? []).map((n) => {
    const short = n.name.replace(/^aks-/, "").replace(/-vmss\d+$/, "");
    return {
      name: short,
      fullName: n.name,
      cpu: n.cpu,
      cpuPct: n.cpu_pct,
      mem: n.memory,
      memPct: n.memory_pct,
      memTotal: n.memory_total ?? "?",
    };
  });

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

  return (
    <div style={{ marginTop: "var(--space-2)" }}>
      {/* Node Resources table — matching ACR card style */}
      {isRunning && nodeMetrics.length > 0 && (
        <div style={{ marginTop: 4 }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
            <thead>
              <tr style={{ borderBottom: "1px solid var(--border-weak)" }}>
                <th
                  style={{
                    textAlign: "left",
                    padding: "4px 0",
                    color: "var(--text-faint)",
                    fontSize: 10,
                    textTransform: "uppercase",
                    fontWeight: 500,
                  }}
                >
                  Node
                  {topQuery.isFetching && (
                    <Loader2
                      size={9}
                      className="spin"
                      style={{ marginLeft: 4, verticalAlign: "middle" }}
                    />
                  )}
                </th>
                <th
                  style={{
                    textAlign: "right",
                    padding: "4px 0",
                    color: "var(--text-faint)",
                    fontSize: 10,
                    textTransform: "uppercase",
                    fontWeight: 500,
                  }}
                >
                  CPU
                </th>
                <th
                  style={{
                    textAlign: "right",
                    padding: "4px 0",
                    color: "var(--text-faint)",
                    fontSize: 10,
                    textTransform: "uppercase",
                    fontWeight: 500,
                  }}
                >
                  Memory
                </th>
              </tr>
            </thead>
            <tbody>
              {nodeMetrics.map((n) => (
                <tr
                  key={n.fullName}
                  style={{ borderBottom: "1px solid var(--border-weak)" }}
                >
                  <td style={{ padding: "5px 0", fontSize: 11 }} title={n.fullName}>
                    <span className="muted">{n.name}</span>
                  </td>
                  <td style={{ padding: "5px 0", textAlign: "right" }}>
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "flex-end",
                        gap: 6,
                      }}
                    >
                      <div
                        style={{
                          width: 48,
                          height: 5,
                          background: "var(--bg-tertiary)",
                          borderRadius: 3,
                          overflow: "hidden",
                        }}
                      >
                        <div
                          style={{
                            width: `${Math.max(n.cpuPct, 2)}%`,
                            height: "100%",
                            background:
                              n.cpuPct > 80
                                ? "var(--danger)"
                                : n.cpuPct > 50
                                  ? "var(--warning)"
                                  : "var(--accent)",
                            borderRadius: 3,
                            transition: "width 0.6s ease",
                          }}
                        />
                      </div>
                      <code
                        style={{
                          fontSize: 10,
                          color: "var(--text-secondary)",
                          minWidth: 50,
                          textAlign: "right",
                        }}
                      >
                        {n.cpu}
                      </code>
                      <span
                        style={{
                          fontSize: 9,
                          color: "var(--text-faint)",
                          minWidth: 28,
                          textAlign: "right",
                        }}
                      >
                        {n.cpuPct}%
                      </span>
                    </div>
                  </td>
                  <td style={{ padding: "5px 0", textAlign: "right" }}>
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "flex-end",
                        gap: 6,
                      }}
                    >
                      <div
                        style={{
                          width: 48,
                          height: 5,
                          background: "var(--bg-tertiary)",
                          borderRadius: 3,
                          overflow: "hidden",
                        }}
                      >
                        <div
                          style={{
                            width: `${Math.max(n.memPct, 2)}%`,
                            height: "100%",
                            background:
                              n.memPct > 80
                                ? "var(--danger)"
                                : n.memPct > 50
                                  ? "var(--warning)"
                                  : "var(--purple, #a78bfa)",
                            borderRadius: 3,
                            transition: "width 0.6s ease",
                          }}
                        />
                      </div>
                      <code
                        style={{
                          fontSize: 10,
                          color: "var(--text-secondary)",
                          minWidth: 50,
                          textAlign: "right",
                        }}
                      >
                        {n.mem}
                      </code>
                      <span
                        style={{
                          fontSize: 9,
                          color: "var(--text-faint)",
                          minWidth: 28,
                          textAlign: "right",
                        }}
                      >
                        {n.memPct}%
                      </span>
                      {n.memTotal && n.memTotal !== "?" && (
                        <span
                          className="muted"
                          style={{ fontSize: 8, minWidth: 30, textAlign: "right" }}
                        >
                          / {n.memTotal}
                        </span>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {isRunning && topQuery.isLoading && nodeMetrics.length === 0 && (
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

      {/* Open modal button */}
      <button
        onClick={() => setShowModal(true)}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 4,
          marginTop: 6,
          background: "none",
          border: "none",
          color: "var(--accent)",
          cursor: "pointer",
          padding: 0,
          fontSize: 10,
        }}
      >
        <Maximize2 size={10} /> View full details
      </button>

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
                maxWidth: 780,
                width: "94vw",
                maxHeight: "88vh",
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
