/**
 * Control Plane Sidecars card — topology view of the in-revision sidecars
 * (frontend, api, worker, beat, redis, terminal) with near-real-time
 * CPU/MEM and an animated traffic pulse along each healthy data path.
 *
 * Data source: useSidecarMetrics() — SSE-pushed snapshots every 5 s via
 * /api/monitor/sidecars/events, with 30 s polling fallback to
 * /api/monitor/sidecars on connection loss.
 *
 * The visual design is the topology proposal from
 * /sidecar-design-preview — see that file's history for the rationale on
 * row layout, the row-spanning RowParticle, and why we settled on a
 * 5-column grid (90px label + 1fr arrow + 168px node + 1fr arrow + 168px
 * node) so left/right node edges align across all four rows.
 */
import {
  ArrowRight,
  Boxes,
  Clock,
  Database,
  Globe,
  Server,
  TerminalSquare,
} from "lucide-react";

import { MonitorCard } from "@/components/MonitorCard";
import {
  useSidecarMetrics,
  type SidecarHealth,
  type SidecarMetric,
  type SidecarsSnapshot,
} from "@/hooks/useSidecarMetrics";

// ---------------------------------------------------------------------------
// Visual constants — keep in sync with the design-preview keyframes.
// ---------------------------------------------------------------------------
const NODE_W = 168;

const HEALTH_LABEL: Record<SidecarHealth, string> = {
  ok: "Healthy",
  degraded: "Degraded",
  down: "Down",
};

const HEALTH_COLOR: Record<SidecarHealth, string> = {
  ok: "var(--success)",
  degraded: "var(--warning)",
  down: "var(--danger)",
};

const ICONS: Record<string, React.ReactNode> = {
  frontend: <Globe size={14} strokeWidth={1.5} />,
  api: <Server size={14} strokeWidth={1.5} />,
  worker: <Boxes size={14} strokeWidth={1.5} />,
  beat: <Clock size={14} strokeWidth={1.5} />,
  redis: <Database size={14} strokeWidth={1.5} />,
  terminal: <TerminalSquare size={14} strokeWidth={1.5} />,
};

const PLACEHOLDER: SidecarMetric = {
  name: "?",
  health: "down",
  ts: null,
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function rollupStatus(snap: SidecarsSnapshot | undefined): "ok" | "loading" | "error" | "unavailable" {
  if (!snap || !snap.sidecars || Object.keys(snap.sidecars).length === 0) return "loading";
  const list = Object.values(snap.sidecars);
  if (list.some((s) => s.health === "down")) return "error";
  if (list.some((s) => s.health === "degraded")) return "unavailable";
  return "ok";
}

function summary(snap: SidecarsSnapshot | undefined): string {
  if (!snap) return "—";
  const list = Object.values(snap.sidecars);
  const ok = list.filter((s) => s.health === "ok").length;
  return `${ok}/${list.length} healthy`;
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------
function StatusDot({ health, size = 8 }: { health: SidecarHealth; size?: number }) {
  return (
    <span
      aria-hidden
      style={{
        width: size,
        height: size,
        borderRadius: 999,
        background: HEALTH_COLOR[health],
        display: "inline-block",
        flexShrink: 0,
        boxShadow: health === "ok" ? `0 0 0 2px rgba(106, 214, 163, 0.18)` : undefined,
      }}
    />
  );
}

function NearRealtimeLabel({ source }: { source: "live" | "polling" | "connecting" }) {
  const label =
    source === "live"
      ? "Near real-time · 5s"
      : source === "polling"
        ? "Polling · 30s"
        : "Connecting…";
  const dotColor = source === "live" ? "var(--accent)" : "var(--text-muted)";
  return (
    <span
      title={
        source === "live"
          ? "SSE stream pushing every 5s from /api/monitor/sidecars/events."
          : source === "polling"
            ? "SSE unavailable — falling back to /api/monitor/sidecars polling."
            : "Acquiring SSE ticket…"
      }
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        fontSize: 10,
        padding: "2px 8px",
        borderRadius: 999,
        background:
          source === "live" ? "rgba(122, 167, 255, 0.08)" : "rgba(154,163,184,0.08)",
        border:
          source === "live"
            ? "1px solid rgba(122, 167, 255, 0.22)"
            : "1px solid var(--border-weak)",
        color: "var(--text-muted)",
        whiteSpace: "nowrap",
      }}
    >
      <span
        aria-hidden
        style={{
          width: 6,
          height: 6,
          borderRadius: 999,
          background: dotColor,
          boxShadow:
            source === "live" ? "0 0 6px rgba(122,167,255,0.55)" : undefined,
        }}
      />
      {label}
    </span>
  );
}

function TopoNode({ s, width = NODE_W }: { s: SidecarMetric; width?: number }) {
  const cpu = s.cpu_pct ?? null;
  const mem = s.mem_pct ?? null;
  return (
    <div
      style={{
        width,
        padding: "10px 12px",
        borderRadius: 12,
        position: "relative",
        zIndex: 1,
        border: `1px solid ${
          s.health === "ok"
            ? "rgba(106,214,163,0.35)"
            : s.health === "degraded"
              ? "rgba(240,198,116,0.45)"
              : "rgba(224,123,138,0.45)"
        }`,
        background: "var(--bg-tertiary)",
        boxShadow:
          s.health === "ok"
            ? "0 0 16px rgba(106,214,163,0.12)"
            : s.health === "degraded"
              ? "0 0 16px rgba(240,198,116,0.12)"
              : "0 0 16px rgba(224,123,138,0.12)",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <StatusDot health={s.health} size={9} />
        <span style={{ color: "var(--text-faint)", display: "flex" }}>
          {ICONS[s.name] ?? <Server size={14} strokeWidth={1.5} />}
        </span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 13, fontWeight: 600, lineHeight: 1.2 }}>{s.name}</div>
          <div style={{ fontSize: 10, color: "var(--text-muted)" }}>
            {HEALTH_LABEL[s.health]}
          </div>
        </div>
      </div>
      <div
        style={{
          marginTop: 8,
          fontSize: 10,
          color: "var(--text-faint)",
          display: "flex",
          justifyContent: "space-between",
        }}
      >
        <span>cpu {cpu == null ? "—" : `${cpu}%`}</span>
        <span>mem {mem == null ? "—" : `${mem}%`}</span>
      </div>
    </div>
  );
}

function TopoArrow({ degraded = false }: { degraded?: boolean }) {
  return (
    <div
      aria-hidden
      style={{
        position: "relative",
        height: 2,
        width: "100%",
        background: degraded
          ? "repeating-linear-gradient(90deg, var(--warning) 0 6px, transparent 6px 10px)"
          : "linear-gradient(90deg, transparent 0%, var(--text-faint) 50%, transparent 100%)",
        overflow: "visible",
      }}
    >
      <ArrowRight
        size={12}
        style={{
          position: "absolute",
          right: -2,
          top: -6,
          color: degraded ? "var(--warning)" : "var(--text-faint)",
        }}
      />
    </div>
  );
}

function RowParticle({
  delaySec = 0,
  durationSec,
  endRight,
}: {
  delaySec?: number;
  durationSec?: number;
  endRight?: string;
}) {
  const style: React.CSSProperties & Record<string, string> = {
    animationDelay: `${delaySec}s`,
  };
  if (endRight) style["--row-end"] = endRight;
  if (durationSec) style.animationDuration = `${durationSec}s`;
  return <span className="topo-row-particle" aria-hidden style={style} />;
}

// ---------------------------------------------------------------------------
// Card
// ---------------------------------------------------------------------------
export function SidecarsCard() {
  const { data, source, lastUpdated } = useSidecarMetrics();
  const sidecars = data?.sidecars ?? {};
  const get = (id: string): SidecarMetric =>
    sidecars[id] ?? { ...PLACEHOLDER, name: id };

  const fe = get("frontend");
  const api = get("api");
  const worker = get("worker");
  const beat = get("beat");
  const redis = get("redis");
  const terminal = get("terminal");

  const gridStyle: React.CSSProperties = {
    display: "grid",
    gridTemplateColumns: `90px minmax(40px, 1fr) ${NODE_W}px minmax(40px, 1fr) ${NODE_W}px`,
    alignItems: "center",
    columnGap: 8,
    padding: "8px 4px",
    position: "relative",
  };
  const labelStyle: React.CSSProperties = {
    fontSize: 10,
    color: "var(--text-faint)",
    textAlign: "right",
  };

  return (
    <MonitorCard
      title="Control Plane Sidecars"
      subtitle={`Data flow inside ca-elb-control · revision ${data?.revision ?? "?"}`}
      status={rollupStatus(data)}
      lastRefreshed={lastUpdated}
      onRefresh={() => {}}
      accentColor="terminal"
      collapsible
      rightSlot={
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <NearRealtimeLabel source={source} />
          <span className="muted" style={{ fontSize: 11 }}>
            {summary(data)}
          </span>
        </div>
      }
    >
      {/* Animation keyframes — mirror of /sidecar-design-preview. Keep
          here so the card is self-contained; once the preview route is
          deleted these can move into web/src/theme/glass.css. */}
      <style>{`
        @keyframes topoRowParticle {
          0%   { left: 98px;                              opacity: 0; }
          5%   { opacity: 1; }
          95%  { opacity: 1; }
          100% { left: var(--row-end, calc(100% - 12px)); opacity: 0; }
        }
        .topo-row-particle {
          position: absolute;
          top: 50%;
          width: 8px;
          height: 8px;
          margin-top: -4px;
          border-radius: 999px;
          background: var(--accent);
          box-shadow: 0 0 12px 2px rgba(122, 167, 255, 0.55);
          pointer-events: none;
          z-index: 0;
          animation: topoRowParticle 1.6s linear infinite;
        }
        @media (prefers-reduced-motion: reduce) {
          .topo-row-particle { animation: none; opacity: 0.6; }
        }
      `}</style>

      {/* Row 1: HTTP path */}
      <div style={gridStyle}>
        <div style={labelStyle}>Browser ↣</div>
        <TopoArrow />
        <TopoNode s={fe} />
        <TopoArrow />
        <TopoNode s={api} />
        {fe.health === "ok" && api.health === "ok" && <RowParticle delaySec={0} />}
      </div>
      <div
        style={{
          fontSize: 10,
          color: "var(--text-faint)",
          paddingLeft: 102,
          marginTop: -4,
          marginBottom: 4,
          fontStyle: "italic",
        }}
      >
        Public ingress lands on <code>api:8080</code>; api reverse-proxies non-
        <code>/api/*</code> requests to <code>frontend:8081</code>.
      </div>

      {/* Row 2: async path — api enqueues, worker pops */}
      <div style={gridStyle}>
        <div style={labelStyle}>Async ↣</div>
        <TopoArrow degraded={worker.health !== "ok" || redis.health !== "ok"} />
        <TopoNode s={redis} />
        <TopoArrow degraded={worker.health !== "ok"} />
        <TopoNode s={worker} />
        {worker.health === "ok" && redis.health === "ok" && <RowParticle delaySec={0.4} />}
      </div>
      <div
        style={{
          fontSize: 10,
          color: "var(--text-faint)",
          paddingLeft: 102,
          marginTop: -4,
          marginBottom: 4,
          fontStyle: "italic",
        }}
      >
        api produces tasks into the broker; worker pops and runs them.
      </div>

      {/* Row 3: scheduler — beat enqueues into the same broker */}
      <div style={gridStyle}>
        <div style={labelStyle}>Scheduled ↣</div>
        <TopoArrow />
        <TopoNode s={beat} />
        <div
          style={{
            gridColumn: "4 / span 2",
            fontSize: 10,
            color: "var(--text-faint)",
            fontStyle: "italic",
            paddingLeft: 14,
          }}
        >
          beat enqueues periodic jobs into the same redis broker.
        </div>
        {beat.health === "ok" && (
          <RowParticle
            delaySec={0.8}
            durationSec={0.9}
            endRight="calc((100% - 458px) / 2 + 250px)"
          />
        )}
      </div>

      {/* Row 4: terminal channel — api proxies WebSocket + privileged exec */}
      <div style={gridStyle}>
        <div style={labelStyle}>ws / exec ↣</div>
        <TopoArrow />
        <TopoNode s={api} />
        <TopoArrow degraded={terminal.health !== "ok"} />
        <TopoNode s={terminal} />
        {terminal.health === "ok" && api.health === "ok" && (
          <RowParticle delaySec={1.2} />
        )}
      </div>
      <div
        style={{
          fontSize: 10,
          color: "var(--text-faint)",
          paddingLeft: 102,
          marginTop: -4,
          marginBottom: 4,
          fontStyle: "italic",
        }}
      >
        api proxies <code>/api/terminal/ws</code> + the privileged exec channel
        on <code>:7682</code>; worker uses the same exec channel for shell-only
        tools (<code>azcopy</code>, <code>kubectl</code>, <code>elastic-blast</code>).
      </div>

      {/* Legend */}
      <div
        className="muted"
        style={{
          marginTop: 8,
          fontSize: 10,
          display: "flex",
          gap: 14,
          flexWrap: "wrap",
        }}
      >
        <span>
          <StatusDot health="ok" /> Healthy
        </span>
        <span>
          <StatusDot health="degraded" /> Degraded
        </span>
        <span>
          <StatusDot health="down" /> Down
        </span>
        <span style={{ color: "var(--warning)" }}>─ ─ ─ degraded link</span>
        <span style={{ color: "var(--accent)" }}>● animated dot = live traffic</span>
      </div>
    </MonitorCard>
  );
}

export default SidecarsCard;
