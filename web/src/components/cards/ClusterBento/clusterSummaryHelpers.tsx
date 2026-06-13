import type { AksClusterSummary } from "@/api/endpoints";
import type { NodeSummary } from "@/components/ClusterDetailModal/useNodeSummary";

/**
 * Shared summary atoms + topology label helpers used by both the live
 * `ClusterBento` render and the `ClusterReadinessBento` fallback render.
 *
 * Extracted from `ClusterBento.tsx` (issue #24 SRP split) so the two render
 * paths reference one definition instead of duplicating it. Presentation +
 * pure label derivation only — no data fetching.
 */

export function emptyNodeSummary(): NodeSummary {
  return {
    total: 0,
    systemCount: 0,
    userCount: 0,
    cpuUsedM: 0,
    cpuTotalM: 0,
    memUsedKi: 0,
    memTotalKi: 0,
    cpuPct: 0,
    memPct: 0,
    notReady: 0,
    hot: 0,
    pressure: [],
  };
}

export function SummaryRow({
  icon,
  label,
  value,
  hint,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
      <span style={{ color: "var(--text-faint)", display: "inline-flex" }}>{icon}</span>
      <span
        style={{
          color: "var(--text-faint)",
          textTransform: "uppercase",
          letterSpacing: "0.08em",
          fontSize: 9,
          fontWeight: 600,
        }}
      >
        {label}
      </span>
      <span
        style={{
          marginLeft: "auto",
          color: "var(--text-primary)",
          fontVariantNumeric: "tabular-nums",
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
        title={hint ? `${value} · ${hint}` : value}
      >
        {value}
        {hint && (
          <span style={{ color: "var(--text-faint)", marginLeft: 6, fontSize: 10 }}>
            · {hint}
          </span>
        )}
      </span>
    </div>
  );
}

/** Live ready user-pool nodes (when k8s reachable) vs configured fallback. */
export function topologyNodesLabel(cluster: AksClusterSummary, ns: NodeSummary): string {
  if (ns.total > 0) {
    return `${ns.userCount + ns.systemCount} ready · user ${ns.userCount}`;
  }
  return cluster.node_count?.toString() ?? "—";
}

export function topologyPoolsLabel(cluster: AksClusterSummary, ns: NodeSummary): string {
  const pools = cluster.agent_pools ?? [];
  if (pools.length > 0) {
    const sys = pools.filter((p) => (p.mode ?? "").toLowerCase() === "system").length;
    const usr = pools.filter((p) => (p.mode ?? "").toLowerCase() === "user").length;
    if (sys + usr > 0) {
      return `system ${sys} · user ${usr}`;
    }
    return pools.length.toString();
  }
  if (ns.total > 0) {
    return `system ${ns.systemCount > 0 ? 1 : 0} · user ${ns.userCount > 0 ? 1 : 0}`;
  }
  return "—";
}
