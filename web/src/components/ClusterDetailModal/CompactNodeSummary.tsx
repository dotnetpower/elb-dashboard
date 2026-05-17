import { AlertTriangle, Loader2, Maximize2 } from "lucide-react";

import { fmtCores, fmtGiB } from "./k8sFormat";
import type { NodeSummary } from "./useNodeSummary";

/**
 * One-line aggregate summary strip — pool dots + CPU/MEM totals + health.
 * Renders as a button so the click-to-expand affordance is discoverable
 * (cursor + keyboard + visible Maximize2 icon).
 */
export function CompactNodeSummary({
  summary,
  isFetching,
  onOpenModal,
}: {
  summary: NodeSummary;
  isFetching: boolean;
  onOpenModal: () => void;
}) {
  const healthy =
    summary.total > 0 && summary.notReady === 0 && summary.pressure.length === 0;

  return (
    <button
      type="button"
      onClick={onOpenModal}
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
      <span className="muted" style={{ fontSize: 11 }}>
        ·
      </span>
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
      <span className="muted" style={{ fontSize: 11 }}>
        ·
      </span>
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
        {isFetching && (
          <Loader2
            size={10}
            className="spin"
            style={{ color: "var(--text-faint)" }}
          />
        )}
        {/* Explicit modal-open affordance so users can tell the whole
            strip is clickable. Sits to the right of the health pill,
            opacity 0.7 when idle for low chrome. */}
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
  );
}
