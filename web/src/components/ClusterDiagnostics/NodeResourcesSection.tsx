import { Loader2, AlertTriangle, ChevronDown } from "lucide-react";
import { useState } from "react";

import type { K8sNodeMetrics } from "@/api/endpoints";

import {
  formatCores,
  formatMemoryGiB,
  isSystemPool,
  poolAccent,
  pressureFlags,
  shortNodeName,
} from "./k8sFormat";
import { SectionShimmerBar } from "./SectionShimmerBar";

/**
 * Visual progress bars for per-node CPU and memory usage, grouped by
 * AKS node pool (System pools first). Reads typed K8s metrics; pure
 * presentation otherwise.
 */
interface NodeResourcesQuery {
  isLoading: boolean;
  isFetching?: boolean;
  isError: boolean;
  data?: { nodes: K8sNodeMetrics[] } | null;
  error?: unknown;
}

export function NodeResourcesSection({ query }: { query: NodeResourcesQuery }) {
  // Default expanded — Node Resources is the most common reason the modal
  // is opened, so showing it on first paint matches the user's intent.
  // Users can still collapse it like Nodes / Active Pods below.
  const [collapsed, setCollapsed] = useState(false);
  const metrics = query.data?.nodes ?? [];

  // True when at least one node reported reclaimable file cache (kubelet
  // /stats/summary reachable). Drives the two-colour legend; when false the
  // bars render working-set-only exactly as before.
  const anyCache = metrics.some(
    (n) => typeof n.cache_ki === "number" && n.cache_ki > 0,
  );

  // Cluster aggregate — sum all nodes for the header summary line.
  const totals = metrics.reduce(
    (acc, n) => {
      acc.cpu_used_m += n.cpu_m ?? 0;
      acc.cpu_total_m += n.cpu_capacity_m ?? 0;
      acc.mem_used_ki += n.mem_ki ?? 0;
      acc.mem_total_ki += n.mem_capacity_ki ?? 0;
      if (n.ready === false) acc.not_ready += 1;
      return acc;
    },
    { cpu_used_m: 0, cpu_total_m: 0, mem_used_ki: 0, mem_total_ki: 0, not_ready: 0 },
  );
  const totalCpuPct =
    totals.cpu_total_m > 0
      ? Math.round((totals.cpu_used_m / totals.cpu_total_m) * 100)
      : 0;
  const totalMemPct =
    totals.mem_total_ki > 0
      ? Math.round((totals.mem_used_ki / totals.mem_total_ki) * 100)
      : 0;

  // Group by pool (System pools first, User pools after) so the rows visually
  // mirror the pool cards above. Within each pool, sort by CPU desc so a hot
  // node bubbles to the top.
  const grouped = new Map<string, K8sNodeMetrics[]>();
  for (const n of metrics) {
    const pool = n.pool || "(unlabelled)";
    const list = grouped.get(pool) ?? [];
    list.push(n);
    grouped.set(pool, list);
  }
  const orderedPools = [...grouped.entries()].sort(([a], [b]) => {
    const sa = isSystemPool(a) ? 0 : 1;
    const sb = isSystemPool(b) ? 0 : 1;
    if (sa !== sb) return sa - sb;
    return a.localeCompare(b);
  });
  for (const list of grouped.values()) {
    list.sort((x, y) => y.cpu_pct - x.cpu_pct);
  }

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
        onClick={() => setCollapsed((v) => !v)}
        style={{
          padding: "8px 12px",
          background: collapsed ? "transparent" : "var(--bg-tertiary)",
          fontSize: 11,
          fontWeight: 500,
          display: "flex",
          alignItems: "center",
          gap: 10,
          borderBottom: collapsed ? "none" : "1px solid var(--border-weak)",
          width: "100%",
          border: "none",
          color: "var(--text-primary)",
          cursor: "pointer",
          textAlign: "left",
        }}
        aria-expanded={!collapsed}
        aria-label={`${collapsed ? "Expand" : "Collapse"} Node Resources`}
      >
        <ChevronDown
          size={12}
          style={{
            transform: collapsed ? "rotate(-90deg)" : "rotate(0deg)",
            color: "var(--text-faint)",
            transition: "transform 0.15s",
          }}
        />
        <span>Node Resources</span>
        {metrics.length > 0 && (
          <span
            style={{
              fontSize: 10,
              fontFamily: "var(--font-mono)",
              color: "var(--text-muted)",
            }}
            title={`${totals.cpu_used_m}m of ${totals.cpu_total_m}m CPU; ${Math.round(totals.mem_used_ki / 1024)}Mi of ${Math.round(totals.mem_total_ki / 1024)}Mi memory`}
          >
            {metrics.length} {metrics.length === 1 ? "node" : "nodes"} ·{" "}
            <span style={{ color: "var(--text-primary)" }}>
              {formatCores(totals.cpu_used_m)}
            </span>
            <span style={{ color: "var(--text-faint)" }}>
              {" "}
              / {formatCores(totals.cpu_total_m)} cores
            </span>{" "}
            <span style={{ color: "var(--text-faint)" }}>({totalCpuPct}%)</span> ·{" "}
            <span style={{ color: "var(--text-primary)" }}>
              {formatMemoryGiB(totals.mem_used_ki)}
            </span>
            <span style={{ color: "var(--text-faint)" }}>
              {" "}
              / {formatMemoryGiB(totals.mem_total_ki)} GiB
            </span>{" "}
            <span style={{ color: "var(--text-faint)" }}>({totalMemPct}%)</span>
            {totals.not_ready > 0 && (
              <span style={{ color: "var(--danger)", marginLeft: 8 }}>
                · {totals.not_ready} NotReady
              </span>
            )}
          </span>
        )}
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
      </button>
      {!collapsed && (
        <div style={{ padding: "10px 12px" }}>
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
            <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
              {anyCache && (
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 14,
                    fontSize: 10,
                    color: "var(--text-muted)",
                  }}
                >
                  <span style={{ display: "flex", alignItems: "center", gap: 5 }}>
                    <span
                      style={{
                        width: 10,
                        height: 6,
                        borderRadius: 2,
                        background: "var(--purple)",
                      }}
                    />
                    In use (working set)
                  </span>
                  <span style={{ display: "flex", alignItems: "center", gap: 5 }}>
                    <span
                      style={{
                        width: 10,
                        height: 6,
                        borderRadius: 2,
                        background: "var(--teal)",
                        opacity: 0.7,
                      }}
                    />
                    Reclaimable file cache (mostly warm DB)
                  </span>
                </div>
              )}
              {orderedPools.map(([pool, rows]) => (
                <div key={pool}>
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 8,
                      fontSize: 10,
                      textTransform: "uppercase",
                      letterSpacing: "0.07em",
                      color: "var(--text-faint)",
                      paddingBottom: 4,
                    }}
                  >
                    <span
                      style={{
                        width: 8,
                        height: 8,
                        borderRadius: 2,
                        background: poolAccent(pool),
                      }}
                    />
                    <span>{isSystemPool(pool) ? "System" : "User"}</span>
                    <span
                      style={{
                        color: "var(--text-muted)",
                        fontFamily: "var(--font-mono)",
                        textTransform: "none",
                        letterSpacing: 0,
                      }}
                    >
                      · {pool} · {rows.length} {rows.length === 1 ? "node" : "nodes"}
                    </span>
                  </div>
                  {rows.map((n) => (
                    <NodeRow key={n.name} metric={n} />
                  ))}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function NodeRow({ metric }: { metric: K8sNodeMetrics }) {
  const cpuColor =
    metric.cpu_pct > 80
      ? "var(--danger)"
      : metric.cpu_pct > 50
        ? "var(--warning)"
        : poolAccent(metric.pool);
  const memColor =
    metric.memory_pct > 80
      ? "var(--danger)"
      : metric.memory_pct > 50
        ? "var(--warning)"
        : "var(--purple)";
  const flags = pressureFlags(metric.conditions);
  const ready = metric.ready !== false;
  const cpuCores = formatCores(metric.cpu_m);
  const cpuTotal = formatCores(metric.cpu_capacity_m);
  const memGiB = formatMemoryGiB(metric.mem_ki);
  const memTotal = formatMemoryGiB(metric.mem_capacity_ki);
  const hasCache = typeof metric.cache_ki === "number" && metric.cache_ki > 0;
  const cacheGiB = hasCache ? formatMemoryGiB(metric.cache_ki) : null;
  const cachePct = hasCache ? (metric.cache_pct ?? 0) : 0;
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "4px minmax(120px, 220px) 1fr 1fr",
        gap: 12,
        alignItems: "center",
        padding: "6px 0",
        borderTop: "1px solid var(--border-weak)",
      }}
    >
      <span
        style={{
          width: 4,
          height: 24,
          borderRadius: 2,
          background: poolAccent(metric.pool),
        }}
        aria-hidden
      />
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          minWidth: 0,
          fontSize: 11,
          fontFamily: "var(--font-mono)",
          color: "var(--text-primary)",
          overflow: "hidden",
        }}
      >
        <span
          title={ready ? "Ready" : "NotReady"}
          style={{
            width: 7,
            height: 7,
            borderRadius: "50%",
            background: ready ? "var(--success)" : "var(--danger)",
            flexShrink: 0,
          }}
        />
        <span
          title={metric.name}
          style={{
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {shortNodeName(metric.name)}
        </span>
        {flags.length > 0 && (
          <span
            title={flags.join(", ")}
            style={{
              fontSize: 9,
              padding: "1px 5px",
              borderRadius: 3,
              background: "rgba(240,114,111,0.18)",
              color: "var(--danger)",
              flexShrink: 0,
            }}
          >
            {flags.length === 1 ? flags[0] : `${flags.length} pressure`}
          </span>
        )}
      </div>
      <UsageBar
        pct={metric.cpu_pct}
        color={cpuColor}
        title={`${metric.cpu} of ${metric.cpu_capacity_m ?? "?"}m`}
        label={
          <>
            <span style={{ color: "var(--text-primary)" }}>{cpuCores}</span>
            <span style={{ color: "var(--text-faint)" }}> / {cpuTotal} cores</span>{" "}
            <span style={{ color: "var(--text-faint)" }}>({metric.cpu_pct}%)</span>
          </>
        }
        labelMinWidth={92}
        labelTitle={`${metric.cpu} (raw ${metric.cpu_m ?? 0}m of ${metric.cpu_capacity_m ?? 0}m)`}
      />
      <UsageBar
        pct={metric.memory_pct}
        color={memColor}
        overlay={
          hasCache ? { pct: cachePct, color: "var(--teal)" } : undefined
        }
        title={`${metric.memory} of ${metric.memory_total}`}
        label={
          <>
            <span style={{ color: "var(--text-primary)" }}>{memGiB}</span>
            <span style={{ color: "var(--text-faint)" }}> / {memTotal} GiB</span>{" "}
            <span style={{ color: "var(--text-faint)" }}>({metric.memory_pct}%)</span>
            {hasCache && (
              <>
                {" "}
                <span style={{ color: "var(--teal)" }}>+{cacheGiB} cache</span>
              </>
            )}
          </>
        }
        labelMinWidth={100}
        labelTitle={
          hasCache
            ? `working set ${metric.memory} (${metric.mem_ki ?? 0}Ki) · reclaimable file cache ${cacheGiB} GiB (${metric.cache_ki ?? 0}Ki, ${cachePct}%) of ${metric.mem_capacity_ki ?? 0}Ki — node-wide page cache dominated by the warmed BLAST DB volumes (also images/logs/other file I/O), so it can exceed the DB's catalogue size. During an active search the DB pages count as working set instead.`
            : `${metric.memory} (raw ${metric.mem_ki ?? 0}Ki of ${metric.mem_capacity_ki ?? 0}Ki)`
        }
      />
    </div>
  );
}

function UsageBar({
  pct,
  color,
  overlay,
  title,
  label,
  labelMinWidth,
  labelTitle,
}: {
  pct: number;
  color: string;
  overlay?: { pct: number; color: string };
  title: string;
  label: React.ReactNode;
  labelMinWidth: number;
  labelTitle: string;
}) {
  const basePct = Math.max(Math.min(pct, 100), pct > 0 ? 4 : 0);
  // Clamp the overlay (cache) to whatever width remains so the two stacked
  // segments never exceed the 100% track.
  const overlayPct = overlay
    ? Math.max(0, Math.min(overlay.pct, 100 - basePct))
    : 0;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
      <div
        style={{
          flex: 1,
          height: 6,
          background: "var(--bg-tertiary)",
          borderRadius: 4,
          overflow: "hidden",
          position: "relative",
          display: "flex",
        }}
        title={title}
      >
        <div
          style={{
            width: `${basePct}%`,
            minWidth: pct > 0 ? 4 : undefined,
            height: "100%",
            background: color,
            transition: "width 0.5s ease-out",
          }}
        />
        {overlay && overlayPct > 0 && (
          <div
            style={{
              width: `${overlayPct}%`,
              height: "100%",
              background: overlay.color,
              opacity: 0.7,
              transition: "width 0.5s ease-out",
            }}
          />
        )}
      </div>
      <span
        style={{
          fontSize: 10,
          fontFamily: "var(--font-mono)",
          minWidth: labelMinWidth,
          textAlign: "right",
          color: "var(--text-muted)",
        }}
        title={labelTitle}
      >
        {label}
      </span>
    </div>
  );
}
