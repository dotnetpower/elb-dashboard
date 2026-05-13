import { useState, useCallback } from "react";
import { createPortal } from "react-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Loader2,
  AlertTriangle,
  ChevronDown,
  Terminal,
  RefreshCw,
  X,
} from "lucide-react";

import { monitoringApi } from "@/api/endpoints";
import { formatApiError } from "@/api/client";
import type { K8sNodeMetrics, K8sNode, K8sPod } from "@/api/endpoints";

// ---------------------------------------------------------------------------
// Modal kubectl sections (fetched on mount)
// ---------------------------------------------------------------------------
export function ClusterModalKubectl({
  subscriptionId,
  resourceGroup,
  clusterName,
  topQuery,
}: {
  subscriptionId: string;
  resourceGroup: string;
  clusterName: string;
  topQuery: {
    isLoading: boolean;
    isError: boolean;
    data?: { nodes: K8sNodeMetrics[] } | null;
    error?: unknown;
    refetch: () => void;
  };
}) {
  const nodesQuery = useQuery({
    queryKey: ["aks-nodes-fast", subscriptionId, resourceGroup, clusterName],
    queryFn: () => monitoringApi.k8sNodes(subscriptionId, resourceGroup, clusterName),
    staleTime: 60_000,
    retry: 1,
  });

  const podsQuery = useQuery({
    queryKey: ["aks-pods-fast", subscriptionId, resourceGroup, clusterName],
    queryFn: () => monitoringApi.k8sPods(subscriptionId, resourceGroup, clusterName),
    staleTime: 60_000,
    retry: 1,
  });

  const [customCmd, setCustomCmd] = useState("");
  const [customResult, setCustomResult] = useState<{
    output: string;
    exit_code: number;
  } | null>(null);
  const [customLoading, setCustomLoading] = useState(false);

  const runCustom = useCallback(async () => {
    if (!customCmd.trim()) return;
    setCustomLoading(true);
    try {
      const result = await monitoringApi.runAksCommand(
        subscriptionId,
        resourceGroup,
        clusterName,
        customCmd.trim(),
      );
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
      <div
        style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}
      >
        <div
          style={{
            fontSize: 11,
            fontWeight: 600,
            display: "flex",
            alignItems: "center",
            gap: 6,
          }}
        >
          <span
            style={{ width: 3, height: 14, borderRadius: 2, background: "var(--teal)" }}
          />
          Cluster Diagnostics
        </div>
        <button
          className="glass-button"
          onClick={() => {
            topQuery.refetch();
            nodesQuery.refetch();
            podsQuery.refetch();
          }}
          style={{
            padding: "4px 10px",
            fontSize: 10,
            display: "flex",
            alignItems: "center",
            gap: 4,
          }}
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
      <K8sPodsSection
        query={podsQuery}
        subscriptionId={subscriptionId}
        resourceGroup={resourceGroup}
        clusterName={clusterName}
      />

      {/* Custom command */}
      <div
        style={{
          borderRadius: 8,
          border: "1px solid var(--border-weak)",
          overflow: "hidden",
        }}
      >
        <div
          style={{
            padding: "8px 12px",
            background: "var(--bg-tertiary)",
            fontSize: 10,
            fontWeight: 500,
            display: "flex",
            alignItems: "center",
            gap: 6,
            borderBottom: "1px solid var(--border-weak)",
          }}
        >
          <Terminal size={12} strokeWidth={1.5} /> Run kubectl command
          <span className="muted" style={{ fontSize: 9, marginLeft: "auto" }}>
            read-only: get, top, describe, logs
          </span>
        </div>
        <div style={{ padding: "10px 12px", display: "flex", gap: 8 }}>
          <input
            type="text"
            value={customCmd}
            onChange={(e) => setCustomCmd(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") runCustom();
            }}
            placeholder="kubectl get svc -A"
            style={{
              flex: 1,
              fontSize: 12,
              padding: "7px 10px",
              background: "var(--bg-canvas)",
              border: "1px solid var(--border-weak)",
              borderRadius: 6,
              color: "var(--text-primary)",
              fontFamily: "var(--font-mono)",
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
          <pre
            style={{
              margin: 0,
              padding: "10px 12px",
              fontSize: 11,
              lineHeight: 1.5,
              background: "var(--bg-canvas)",
              borderTop: "1px solid var(--border-weak)",
              overflow: "auto",
              maxHeight: 250,
              whiteSpace: "pre-wrap",
              wordBreak: "break-all",
              color:
                customResult.exit_code === 0 ? "var(--text-primary)" : "var(--danger)",
              fontFamily: "var(--font-mono)",
            }}
          >
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

function NodeResourcesSection({
  query,
}: {
  query: {
    isLoading: boolean;
    isError: boolean;
    data?: { nodes: K8sNodeMetrics[] } | null;
    error?: unknown;
  };
}) {
  const metrics = query.data?.nodes ?? [];

  const shortName = (n: string) => n.replace(/^aks-/, "").replace(/-vmss/, "-");

  return (
    <div
      style={{
        borderRadius: 8,
        border: "1px solid var(--border-weak)",
        overflow: "hidden",
      }}
    >
      <div
        style={{
          padding: "8px 12px",
          background: "var(--bg-tertiary)",
          fontSize: 11,
          fontWeight: 500,
          display: "flex",
          alignItems: "center",
          gap: 6,
          borderBottom: "1px solid var(--border-weak)",
        }}
      >
        Node Resources
        {query.isLoading && (
          <Loader2
            size={10}
            className="spin"
            style={{ marginLeft: "auto", color: "var(--accent)" }}
          />
        )}
        {!query.isLoading && metrics.length > 0 && (
          <span style={{ marginLeft: "auto", fontSize: 9, color: "var(--success)" }}>
            ✓
          </span>
        )}
      </div>
      <div style={{ padding: "12px 14px" }}>
        {query.isLoading && metrics.length === 0 && (
          <div
            className="muted"
            style={{ fontSize: 11, display: "flex", alignItems: "center", gap: 6 }}
          >
            <Loader2 size={12} className="spin" /> Fetching node metrics...
          </div>
        )}
        {query.isError && (
          <div
            style={{
              fontSize: 11,
              color: "var(--text-muted)",
              display: "flex",
              alignItems: "center",
              gap: 6,
            }}
          >
            <AlertTriangle size={12} style={{ color: "var(--warning)" }} />
            Node metrics unavailable — the cluster API may still be warming up. Try
            Refresh All in a moment.
          </div>
        )}
        {metrics.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {/* Header */}
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "1fr 1fr",
                gap: 16,
                paddingLeft: 140,
              }}
            >
              <div
                className="muted"
                style={{
                  fontSize: 9,
                  textTransform: "uppercase",
                  letterSpacing: "0.05em",
                  display: "flex",
                  alignItems: "center",
                  gap: 4,
                }}
              >
                <span
                  style={{
                    width: 8,
                    height: 8,
                    borderRadius: 2,
                    background: "var(--accent)",
                  }}
                />{" "}
                CPU
              </div>
              <div
                className="muted"
                style={{
                  fontSize: 9,
                  textTransform: "uppercase",
                  letterSpacing: "0.05em",
                  display: "flex",
                  alignItems: "center",
                  gap: 4,
                }}
              >
                <span
                  style={{
                    width: 8,
                    height: 8,
                    borderRadius: 2,
                    background: "var(--purple)",
                  }}
                />{" "}
                Memory
              </div>
            </div>
            {metrics.map((n) => (
              <div
                key={n.name}
                style={{
                  display: "grid",
                  gridTemplateColumns: "140px 1fr 1fr",
                  gap: 16,
                  alignItems: "center",
                }}
              >
                <span
                  style={{
                    fontSize: 10,
                    fontFamily: "var(--font-mono)",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                  title={n.name}
                >
                  {shortName(n.name)}
                </span>
                {/* CPU bar */}
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <div
                    style={{
                      flex: 1,
                      height: 8,
                      background: "var(--bg-tertiary)",
                      borderRadius: 4,
                      overflow: "hidden",
                      position: "relative",
                    }}
                  >
                    <div
                      style={{
                        width: `${Math.max(n.cpu_pct, 2)}%`,
                        height: "100%",
                        borderRadius: 4,
                        background:
                          n.cpu_pct > 80
                            ? "var(--danger)"
                            : n.cpu_pct > 50
                              ? "var(--warning)"
                              : "var(--accent)",
                        transition: "width 0.5s ease-out",
                      }}
                    />
                  </div>
                  <span
                    style={{
                      fontSize: 10,
                      fontFamily: "var(--font-mono)",
                      minWidth: 50,
                      textAlign: "right",
                      color: "var(--text-muted)",
                    }}
                  >
                    {n.cpu}{" "}
                    <span style={{ color: "var(--text-faint)" }}>({n.cpu_pct}%)</span>
                  </span>
                </div>
                {/* Memory bar */}
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <div
                    style={{
                      flex: 1,
                      height: 8,
                      background: "var(--bg-tertiary)",
                      borderRadius: 4,
                      overflow: "hidden",
                      position: "relative",
                    }}
                  >
                    <div
                      style={{
                        width: `${Math.max(n.memory_pct, 2)}%`,
                        height: "100%",
                        borderRadius: 4,
                        background:
                          n.memory_pct > 80
                            ? "var(--danger)"
                            : n.memory_pct > 50
                              ? "var(--warning)"
                              : "var(--purple)",
                        transition: "width 0.5s ease-out",
                      }}
                    />
                  </div>
                  <span
                    style={{
                      fontSize: 10,
                      fontFamily: "var(--font-mono)",
                      minWidth: 60,
                      textAlign: "right",
                      color: "var(--text-muted)",
                    }}
                  >
                    {n.memory}{" "}
                    <span style={{ color: "var(--text-faint)" }}>({n.memory_pct}%)</span>
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
function K8sNodesSection({
  query,
}: {
  query: {
    isLoading: boolean;
    isError: boolean;
    data?: { nodes: K8sNode[] } | null;
    error?: unknown;
  };
}) {
  const [collapsed, setCollapsed] = useState(true);
  const nodes = query.data?.nodes ?? [];
  const sc = (s: string) => (s === "Ready" ? "var(--success)" : "var(--danger)");
  return (
    <div
      style={{
        borderRadius: 8,
        border: "1px solid var(--border-weak)",
        overflow: "hidden",
      }}
    >
      <button
        onClick={() => {
          setCollapsed(!collapsed);
        }}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          width: "100%",
          background: collapsed ? "transparent" : "var(--bg-tertiary)",
          border: "none",
          color: "var(--text-primary)",
          cursor: "pointer",
          padding: "8px 12px",
          fontSize: 11,
          textAlign: "left",
          fontWeight: 500,
        }}
      >
        <ChevronDown
          size={12}
          style={{
            transform: collapsed ? "rotate(-90deg)" : "rotate(0deg)",
            color: "var(--text-faint)",
            transition: "transform 0.15s",
          }}
        />
        Nodes
        {nodes.length > 0 && (
          <span className="muted" style={{ fontSize: 9 }}>
            {nodes.length}
          </span>
        )}
        {query.isLoading && (
          <Loader2
            size={10}
            className="spin"
            style={{ marginLeft: "auto", color: "var(--accent)" }}
          />
        )}
        {!query.isLoading && nodes.length > 0 && (
          <span style={{ marginLeft: "auto", fontSize: 9, color: "var(--success)" }}>
            ✓
          </span>
        )}
      </button>
      {!collapsed && (
        <div style={{ borderTop: "1px solid var(--border-weak)", overflowX: "auto" }}>
          {query.isLoading && (
            <div style={{ padding: 16, textAlign: "center" }} className="muted">
              <Loader2 size={14} className="spin" /> Loading...
            </div>
          )}
          {query.isError && (
            <div style={{ padding: 12, fontSize: 11, color: "var(--danger)" }}>
              {formatApiError(query.error, "aks")}
            </div>
          )}
          {nodes.length > 0 && (
            <table
              style={{
                width: "100%",
                fontSize: 10,
                borderCollapse: "collapse",
                fontFamily: "var(--font-mono)",
              }}
            >
              <thead>
                <tr style={{ background: "var(--bg-tertiary)" }}>
                  {["NAME", "STATUS", "VERSION", "IP", "OS", "RUNTIME"].map((h) => (
                    <th
                      key={h}
                      style={{
                        textAlign: "left",
                        padding: "6px 8px",
                        color: "var(--text-faint)",
                        fontSize: 9,
                        textTransform: "uppercase",
                        fontWeight: 500,
                      }}
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {nodes.map((n, i) => (
                  <tr
                    key={n.name}
                    style={{
                      background: i % 2 === 0 ? "transparent" : "rgba(255,255,255,0.012)",
                      borderTop: "1px solid var(--border-weak)",
                    }}
                  >
                    <td style={{ padding: "5px 8px", fontWeight: 500 }}>{n.name}</td>
                    <td style={{ padding: "5px 8px", color: sc(n.status) }}>
                      <span
                        style={{
                          display: "inline-block",
                          width: 5,
                          height: 5,
                          borderRadius: "50%",
                          background: sc(n.status),
                          marginRight: 4,
                          verticalAlign: "middle",
                        }}
                      />
                      {n.status}
                    </td>
                    <td style={{ padding: "5px 8px" }}>{n.version}</td>
                    <td style={{ padding: "5px 8px" }}>{n.internal_ip}</td>
                    <td style={{ padding: "5px 8px" }}>{n.os_image}</td>
                    <td style={{ padding: "5px 8px" }}>{n.runtime}</td>
                  </tr>
                ))}
              </tbody>
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
function K8sPodsSection({
  query,
  subscriptionId,
  resourceGroup,
  clusterName,
}: {
  query: {
    isLoading: boolean;
    isError: boolean;
    data?: { pods: K8sPod[] } | null;
    error?: unknown;
  };
  subscriptionId: string;
  resourceGroup: string;
  clusterName: string;
}) {
  const [collapsed, setCollapsed] = useState(true);
  const [logTarget, setLogTarget] = useState<{ namespace: string; pod: string } | null>(
    null,
  );
  const [logOutput, setLogOutput] = useState<string | null>(null);
  const [logLoading, setLogLoading] = useState(false);
  const pods = query.data?.pods ?? [];
  const sc = (s: string) => {
    const v = s.toLowerCase();
    return v === "running"
      ? "var(--success)"
      : v.includes("error") || v.includes("crash")
        ? "var(--danger)"
        : "var(--warning)";
  };
  const fetchLogs = useCallback(
    async (ns: string, pod: string) => {
      setLogTarget({ namespace: ns, pod });
      setLogOutput(null);
      setLogLoading(true);
      try {
        const r = await monitoringApi.k8sPodLogs(
          subscriptionId,
          resourceGroup,
          clusterName,
          ns,
          pod,
          200,
        );
        setLogOutput(r.logs || "(empty)");
      } catch (e) {
        setLogOutput(`Error: ${(e as Error).message}`);
      } finally {
        setLogLoading(false);
      }
    },
    [subscriptionId, resourceGroup, clusterName],
  );
  return (
    <div
      style={{
        borderRadius: 8,
        border: "1px solid var(--border-weak)",
        overflow: "hidden",
      }}
    >
      <button
        onClick={() => {
          setCollapsed(!collapsed);
        }}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          width: "100%",
          background: collapsed ? "transparent" : "var(--bg-tertiary)",
          border: "none",
          color: "var(--text-primary)",
          cursor: "pointer",
          padding: "8px 12px",
          fontSize: 11,
          textAlign: "left",
          fontWeight: 500,
        }}
      >
        <ChevronDown
          size={12}
          style={{
            transform: collapsed ? "rotate(-90deg)" : "rotate(0deg)",
            color: "var(--text-faint)",
            transition: "transform 0.15s",
          }}
        />
        Active Pods
        {pods.length > 0 && (
          <span className="muted" style={{ fontSize: 9 }}>
            {pods.length}
          </span>
        )}
        {query.isLoading && (
          <Loader2
            size={10}
            className="spin"
            style={{ marginLeft: "auto", color: "var(--accent)" }}
          />
        )}
        {!query.isLoading && pods.length > 0 && (
          <span style={{ marginLeft: "auto", fontSize: 9, color: "var(--success)" }}>
            ✓
          </span>
        )}
      </button>
      {!collapsed && (
        <div style={{ borderTop: "1px solid var(--border-weak)", overflowX: "auto" }}>
          {query.isLoading && (
            <div style={{ padding: 16, textAlign: "center" }} className="muted">
              <Loader2 size={14} className="spin" /> Loading...
            </div>
          )}
          {query.isError && (
            <div style={{ padding: 12, fontSize: 11, color: "var(--danger)" }}>
              {formatApiError(query.error, "aks")}
            </div>
          )}
          {pods.length > 0 && (
            <table
              style={{
                width: "100%",
                fontSize: 10,
                borderCollapse: "collapse",
                fontFamily: "var(--font-mono)",
              }}
            >
              <thead>
                <tr style={{ background: "var(--bg-tertiary)" }}>
                  {["NS", "NAME", "READY", "STATUS", "RESTARTS", "NODE", ""].map((h) => (
                    <th
                      key={h}
                      style={{
                        textAlign: "left",
                        padding: "6px 8px",
                        color: "var(--text-faint)",
                        fontSize: 9,
                        textTransform: "uppercase",
                        fontWeight: 500,
                      }}
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {pods.map((p, i) => (
                  <tr
                    key={`${p.namespace}/${p.name}`}
                    style={{
                      background: i % 2 === 0 ? "transparent" : "rgba(255,255,255,0.012)",
                      borderTop: "1px solid var(--border-weak)",
                    }}
                  >
                    <td
                      style={{
                        padding: "5px 8px",
                        color: "var(--text-muted)",
                        fontSize: 9,
                      }}
                    >
                      {p.namespace}
                    </td>
                    <td style={{ padding: "5px 8px", fontWeight: 500 }}>{p.name}</td>
                    <td style={{ padding: "5px 8px" }}>{p.ready}</td>
                    <td style={{ padding: "5px 8px", color: sc(p.status) }}>
                      <span
                        style={{
                          display: "inline-block",
                          width: 5,
                          height: 5,
                          borderRadius: "50%",
                          background: sc(p.status),
                          marginRight: 4,
                          verticalAlign: "middle",
                        }}
                      />
                      {p.status}
                    </td>
                    <td style={{ padding: "5px 8px" }}>{p.restarts}</td>
                    <td
                      style={{
                        padding: "5px 8px",
                        color: "var(--text-muted)",
                        fontSize: 9,
                      }}
                    >
                      {p.node?.split("-vmss")[0]}
                    </td>
                    <td style={{ padding: "4px 8px" }}>
                      <button
                        className="glass-button"
                        onClick={() => fetchLogs(p.namespace, p.name)}
                        style={{
                          fontSize: 9,
                          padding: "2px 6px",
                          display: "flex",
                          alignItems: "center",
                          gap: 3,
                        }}
                        title={`Logs: ${p.name}`}
                      >
                        <Terminal size={9} /> Logs
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
      {logTarget &&
        createPortal(
          <div
            className="glass-dialog-backdrop"
            onClick={(e) => {
              if (e.target === e.currentTarget) {
                setLogTarget(null);
                setLogOutput(null);
              }
            }}
            role="dialog"
            aria-modal="true"
            aria-label={`Logs: ${logTarget.pod}`}
          >
            <div
              className="glass-card glass-card--strong glass-dialog"
              onClick={(e) => e.stopPropagation()}
              style={{
                maxWidth: 1100,
                width: "calc(100vw - 48px)",
                maxHeight: "90vh",
                display: "flex",
                flexDirection: "column",
                padding: 0,
                overflow: "hidden",
                textAlign: "left",
              }}
            >
              <div
                style={{
                  padding: "14px 20px",
                  background:
                    "linear-gradient(135deg, rgba(92,202,180,0.08) 0%, rgba(110,159,255,0.06) 100%)",
                  borderBottom: "1px solid var(--border-weak)",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <div
                    style={{
                      width: 28,
                      height: 28,
                      borderRadius: 8,
                      background: "linear-gradient(135deg, var(--teal), var(--accent))",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      boxShadow: "0 2px 8px rgba(92,202,180,0.25)",
                    }}
                  >
                    <Terminal size={14} style={{ color: "#fff" }} />
                  </div>
                  <div>
                    <div style={{ fontSize: 13, fontWeight: 600 }}>Pod Logs</div>
                    <div style={{ fontSize: 10, color: "var(--text-muted)" }}>
                      {logTarget.namespace} / {logTarget.pod} · last 200 lines
                    </div>
                  </div>
                </div>
                <div style={{ display: "flex", gap: 6 }}>
                  <button
                    className="glass-button"
                    onClick={() => fetchLogs(logTarget.namespace, logTarget.pod)}
                    disabled={logLoading}
                    style={{
                      padding: "5px 10px",
                      fontSize: 10,
                      display: "flex",
                      alignItems: "center",
                      gap: 4,
                    }}
                  >
                    <RefreshCw size={11} className={logLoading ? "spin" : ""} /> Refresh
                  </button>
                  <button
                    className="glass-button"
                    onClick={() => {
                      setLogTarget(null);
                      setLogOutput(null);
                    }}
                    style={{ padding: "5px 8px", border: "none" }}
                  >
                    <X size={16} />
                  </button>
                </div>
              </div>
              <div
                style={{
                  margin: 0,
                  padding: "14px 20px",
                  flex: 1,
                  overflow: "auto",
                  fontSize: 11,
                  lineHeight: 1.7,
                  background: "#0d1117",
                  fontFamily: "var(--font-mono)",
                  color: "#c9d1d9",
                  textAlign: "left",
                }}
              >
                {logLoading ? (
                  <span style={{ color: "var(--text-faint)" }}>Fetching logs...</span>
                ) : (
                  <LogHighlighter text={logOutput ?? ""} />
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
// Log syntax highlighter — lnav-style colorized log output
// ---------------------------------------------------------------------------
const LOG_COLORS = {
  timestamp: "#6cb6ff", // blue — ISO dates, timestamps
  error: "#f47067", // red — ERROR, FATAL, CRITICAL, panic, fail
  warn: "#f0c674", // yellow — WARN, WARNING
  info: "#57ab5a", // green — INFO
  debug: "#986ee2", // purple — DEBUG, TRACE
  number: "#d2a8ff", // light purple — numbers, durations
  ip: "#6cb6ff", // blue — IP addresses
  path: "#96d0ff", // light blue — file paths
  key: "#e3b341", // golden — key= patterns
  string: "#a5d6ff", // cyan — quoted strings
  dim: "#545d68", // dim — separators, brackets
} as const;

function LogHighlighter({ text }: { text: string }) {
  if (!text) return <span style={{ color: "var(--text-faint)" }}>(empty log)</span>;

  const lines = text.split("\n");
  return (
    <>
      {lines.map((line, i) => (
        <div key={i} style={{ minHeight: "1.7em", display: "flex" }}>
          <span
            style={{
              color: LOG_COLORS.dim,
              userSelect: "none",
              minWidth: 36,
              textAlign: "right",
              paddingRight: 12,
              fontSize: 9,
              lineHeight: "1.7em",
            }}
          >
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
const _LOG_TOKEN_RE =
  /(\d{4}[-/]\d{2}[-/]\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)|(\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?::\d+)?\b)|(\b(?:ERROR|FATAL|CRITICAL|PANIC|EXCEPTION)\b)|(\b(?:WARN(?:ING)?)\b)|(\b(?:INFO)\b)|(\b(?:DEBUG|TRACE)\b)|("[^"]*"|'[^']*')|(\/[\w./\-]+(?:\.\w+))|(\b\w+(?:[-_]\w+)*=)|(\b\d+(?:\.\d+)?(?:m|Mi|Gi|Ki|ms|s|%|ns|us|µs)?\b)/gi;

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
      parts.push(
        <span
          key={key++}
          style={
            isError
              ? { color: "#ffa198" }
              : isWarn
                ? { color: "#e3b341", opacity: 0.85 }
                : undefined
          }
        >
          {before}
        </span>,
      );
    }

    const [fullMatch, ts, ip, err, warn, info, debug, str, path, kv, num] = match;
    let color = "#c9d1d9";
    let fontWeight: number | undefined;

    if (ts) color = LOG_COLORS.timestamp;
    else if (ip) color = LOG_COLORS.ip;
    else if (err) {
      color = LOG_COLORS.error;
      fontWeight = 700;
    } else if (warn) {
      color = LOG_COLORS.warn;
      fontWeight = 600;
    } else if (info) color = LOG_COLORS.info;
    else if (debug) color = LOG_COLORS.debug;
    else if (str) color = LOG_COLORS.string;
    else if (path) color = LOG_COLORS.path;
    else if (kv) color = LOG_COLORS.key;
    else if (num) color = LOG_COLORS.number;

    parts.push(
      <span key={key++} style={{ color, fontWeight }}>
        {fullMatch}
      </span>,
    );
    lastIdx = match.index + fullMatch!.length;
  }

  // Remaining text
  if (lastIdx < line.length) {
    const rest = line.slice(lastIdx);
    parts.push(
      <span
        key={key++}
        style={
          isError
            ? { color: "#ffa198" }
            : isWarn
              ? { color: "#e3b341", opacity: 0.85 }
              : undefined
        }
      >
        {rest}
      </span>,
    );
  }

  return parts;
}
