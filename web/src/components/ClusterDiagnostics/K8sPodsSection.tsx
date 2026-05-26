import { useState, useCallback } from "react";
import { ChevronDown, FileText, Loader2, Terminal } from "lucide-react";

import { monitoringApi } from "@/api/endpoints";
import { formatApiError } from "@/api/client";
import type { K8sPod } from "@/api/endpoints";

import { formatAge } from "./k8sFormat";
import { PodDescribeDialog } from "./PodDescribeDialog";
import { PodLogsDialog } from "./PodLogsDialog";
import { SectionShimmerBar } from "./SectionShimmerBar";

/**
 * Collapsible table of active pods with a one-click "Logs" action that
 * pops the `PodLogsDialog`. Owns the log-fetch lifecycle (target + output
 * + loading) so the dialog itself stays presentation-only.
 */
interface K8sPodsQuery {
  isLoading: boolean;
  isFetching?: boolean;
  isError: boolean;
  data?: { pods: K8sPod[] } | null;
  error?: unknown;
}

export function K8sPodsSection({
  query,
  subscriptionId,
  resourceGroup,
  clusterName,
}: {
  query: K8sPodsQuery;
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
  const [describeTarget, setDescribeTarget] = useState<{
    namespace: string;
    pod: string;
  } | null>(null);
  const [describeOutput, setDescribeOutput] = useState<string | null>(null);
  const [describeLoading, setDescribeLoading] = useState(false);
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
  const closeLogs = () => {
    setLogTarget(null);
    setLogOutput(null);
  };
  const fetchDescribe = useCallback(
    async (ns: string, pod: string) => {
      setDescribeTarget({ namespace: ns, pod });
      setDescribeOutput(null);
      setDescribeLoading(true);
      try {
        const r = await monitoringApi.k8sPodDescribe(
          subscriptionId,
          resourceGroup,
          clusterName,
          ns,
          pod,
        );
        setDescribeOutput(r.describe || "(empty)");
      } catch (e) {
        setDescribeOutput(`Error: ${(e as Error).message}`);
      } finally {
        setDescribeLoading(false);
      }
    },
    [subscriptionId, resourceGroup, clusterName],
  );
  const closeDescribe = () => {
    setDescribeTarget(null);
    setDescribeOutput(null);
  };
  return (
    <div
      style={{
        position: "relative",
        borderRadius: 8,
        border: "1px solid var(--border-weak)",
        overflow: "hidden",
      }}
    >
      <SectionShimmerBar active={Boolean(query.isFetching)} />
      <button
        onClick={() => setCollapsed(!collapsed)}
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
        <div
          className="k8s-pods-table-wrap"
          style={{ borderTop: "1px solid var(--border-weak)", overflowX: "auto" }}
        >
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
                  {["NS", "NAME", "READY", "STATUS", "RESTARTS", "AGE", "NODE", ""].map(
                    (h) => (
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
                    ),
                  )}
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
                        whiteSpace: "nowrap",
                      }}
                      title={p.age || undefined}
                    >
                      {formatAge(p.age)}
                    </td>
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
                      <div style={{ display: "inline-flex", gap: 4 }}>
                        <button
                          className="glass-button k8s-pods-logs-button"
                          onClick={() => fetchLogs(p.namespace, p.name)}
                          style={{
                            fontSize: 9,
                            padding: "2px 6px",
                            display: "inline-flex",
                            alignItems: "center",
                            gap: 3,
                            whiteSpace: "nowrap",
                          }}
                          title={`Logs: ${p.name}`}
                        >
                          <Terminal size={9} /> Logs
                        </button>
                        <button
                          className="glass-button k8s-pods-describe-button"
                          onClick={() => fetchDescribe(p.namespace, p.name)}
                          style={{
                            fontSize: 9,
                            padding: "2px 6px",
                            display: "inline-flex",
                            alignItems: "center",
                            gap: 3,
                            whiteSpace: "nowrap",
                          }}
                          title={`Describe: ${p.name}`}
                        >
                          <FileText size={9} /> Describe
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
      {logTarget && (
        <PodLogsDialog
          target={logTarget}
          output={logOutput}
          loading={logLoading}
          onRefresh={() => fetchLogs(logTarget.namespace, logTarget.pod)}
          onClose={closeLogs}
        />
      )}
      {describeTarget && (
        <PodDescribeDialog
          target={describeTarget}
          output={describeOutput}
          loading={describeLoading}
          onRefresh={() =>
            fetchDescribe(describeTarget.namespace, describeTarget.pod)
          }
          onClose={closeDescribe}
        />
      )}
    </div>
  );
}
