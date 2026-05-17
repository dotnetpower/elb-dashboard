import { useState } from "react";
import { ChevronDown, Loader2 } from "lucide-react";

import { formatApiError } from "@/api/client";
import type { K8sNode } from "@/api/endpoints";

/**
 * Collapsible table of K8s node identities (status / version / IP / OS /
 * runtime). Sourced from the typed direct K8s API. No metrics here —
 * see `NodeResourcesSection` for CPU/mem bars.
 */
interface K8sNodesQuery {
  isLoading: boolean;
  isError: boolean;
  data?: { nodes: K8sNode[] } | null;
  error?: unknown;
}

export function K8sNodesSection({ query }: { query: K8sNodesQuery }) {
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
