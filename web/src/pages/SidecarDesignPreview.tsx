/**
 * Sidecar Status — design preview page (3 proposals).
 *
 * Mounted at /sidecar-design-preview. Mock data only. The proposals are:
 *   1. "Compact strip"  — single MonitorCard with horizontal sidecar pills
 *   2. "Detailed grid"  — MonitorCard with a 6-up grid of mini-cards
 *   3. "Topology view"  — service mesh-style box-and-arrow layout
 *
 * Picking a proposal? The component returning the picked design is at the
 * bottom of this file. Drop the chosen one into Dashboard.tsx alongside
 * the other cards. Wiring to live data needs a new
 *   GET /api/monitor/sidecars
 * endpoint that combines:
 *   - /api/health/ready           (already returns redis + azure_credential
 *                                  + terminal_sidecar from inside the api)
 *   - Azure ContainerApps SDK     (revision/replica state, restart counts)
 *   - Application Insights query  (CPU% / memory% per container)
 */
import { useState } from "react";
import { Link } from "react-router-dom";
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  Boxes,
  Clock,
  Cpu,
  Database,
  ExternalLink,
  FileText,
  Globe,
  HardDrive,
  RefreshCw,
  Server,
  TerminalSquare,
  Zap,
} from "lucide-react";

import { MonitorCard } from "@/components/MonitorCard";

// ---------------------------------------------------------------------------
// Mock data
// ---------------------------------------------------------------------------
type SidecarHealth = "ok" | "degraded" | "down" | "starting";

interface SidecarSnapshot {
  id: string;
  name: string;
  role: string;
  port: string;
  health: SidecarHealth;
  cpuPct: number;
  memPct: number;
  memMiB: number;
  restarts: number;
  uptimeMin: number;
  imageDigest: string;
  icon: React.ReactNode;
}

const MOCK_SIDECARS: SidecarSnapshot[] = [
  {
    id: "frontend",
    name: "frontend",
    role: "nginx — SPA static assets",
    port: "127.0.0.1:8081",
    health: "ok",
    cpuPct: 1,
    memPct: 4,
    memMiB: 18,
    restarts: 0,
    uptimeMin: 1483,
    imageDigest: "sha256:1a2b…f0",
    icon: <Globe size={14} strokeWidth={1.5} />,
  },
  {
    id: "api",
    name: "api",
    role: "FastAPI — uvicorn",
    port: "0.0.0.0:8080",
    health: "ok",
    cpuPct: 8,
    memPct: 22,
    memMiB: 220,
    restarts: 0,
    uptimeMin: 1483,
    imageDigest: "sha256:9c3d…71",
    icon: <Server size={14} strokeWidth={1.5} />,
  },
  {
    id: "worker",
    name: "worker",
    role: "Celery — task executor",
    port: "—",
    health: "degraded",
    cpuPct: 3,
    memPct: 18,
    memMiB: 180,
    restarts: 2,
    uptimeMin: 41,
    imageDigest: "sha256:9c3d…71",
    icon: <Boxes size={14} strokeWidth={1.5} />,
  },
  {
    id: "beat",
    name: "beat",
    role: "Celery beat — scheduler",
    port: "—",
    health: "ok",
    cpuPct: 1,
    memPct: 9,
    memMiB: 86,
    restarts: 0,
    uptimeMin: 1483,
    imageDigest: "sha256:9c3d…71",
    icon: <Clock size={14} strokeWidth={1.5} />,
  },
  {
    id: "redis",
    name: "redis",
    role: "broker + result backend",
    port: "127.0.0.1:6379",
    health: "ok",
    cpuPct: 1,
    memPct: 6,
    memMiB: 28,
    restarts: 0,
    uptimeMin: 1483,
    imageDigest: "sha256:abf2…dd",
    icon: <Database size={14} strokeWidth={1.5} />,
  },
  {
    id: "terminal",
    name: "terminal",
    role: "ttyd + elastic-blast toolchain",
    port: "127.0.0.1:7681",
    health: "down",
    cpuPct: 0,
    memPct: 0,
    memMiB: 0,
    restarts: 4,
    uptimeMin: 0,
    imageDigest: "sha256:e771…02",
    icon: <TerminalSquare size={14} strokeWidth={1.5} />,
  },
];

// ---------------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------------
const HEALTH_COLOR: Record<SidecarHealth, string> = {
  ok: "var(--success)",
  degraded: "var(--warning)",
  down: "var(--danger)",
  starting: "var(--text-muted)",
};

const HEALTH_LABEL: Record<SidecarHealth, string> = {
  ok: "Healthy",
  degraded: "Degraded",
  down: "Down",
  starting: "Starting",
};

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
        boxShadow:
          health === "ok" ? `0 0 0 2px rgba(106, 214, 163, 0.18)` : undefined,
      }}
    />
  );
}

function formatUptime(min: number): string {
  if (min === 0) return "—";
  if (min < 60) return `${min}m`;
  const hours = Math.floor(min / 60);
  if (hours < 24) return `${hours}h ${min % 60}m`;
  const days = Math.floor(hours / 24);
  return `${days}d ${hours % 24}h`;
}

function rollupStatus(snapshots: SidecarSnapshot[]): "ok" | "loading" | "error" | "unavailable" {
  if (snapshots.some((s) => s.health === "down")) return "error";
  if (snapshots.some((s) => s.health === "degraded")) return "unavailable";
  return "ok";
}

function summary(snapshots: SidecarSnapshot[]): string {
  const ok = snapshots.filter((s) => s.health === "ok").length;
  const total = snapshots.length;
  return `${ok}/${total} healthy`;
}

// ---------------------------------------------------------------------------
// Proposal 1 — Compact strip
// ---------------------------------------------------------------------------
function ProposalCompactStrip({ snapshots }: { snapshots: SidecarSnapshot[] }) {
  return (
    <MonitorCard
      title="Container App sidecars"
      subtitle="ca-elb-control · revision r0042"
      status={rollupStatus(snapshots)}
      lastRefreshed={new Date()}
      onRefresh={() => {}}
      accentColor="terminal"
      collapsible
      rightSlot={
        <span className="muted" style={{ fontSize: 11 }}>
          {summary(snapshots)}
        </span>
      }
    >
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))",
          gap: 8,
        }}
      >
        {snapshots.map((s) => (
          <div
            key={s.id}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 10,
              padding: "10px 12px",
              borderRadius: 10,
              background: "var(--bg-tertiary)",
              border: "1px solid var(--border-weak)",
            }}
          >
            <StatusDot health={s.health} />
            <span style={{ color: "var(--text-faint)", display: "flex" }}>{s.icon}</span>
            <div style={{ minWidth: 0, flex: 1 }}>
              <div style={{ fontSize: 12, fontWeight: 600, lineHeight: 1.2 }}>
                {s.name}
              </div>
              <div
                style={{
                  fontSize: 10,
                  color: "var(--text-muted)",
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                }}
              >
                {HEALTH_LABEL[s.health]} · {formatUptime(s.uptimeMin)}
              </div>
            </div>
          </div>
        ))}
      </div>

      <div
        className="muted"
        style={{
          marginTop: 12,
          fontSize: 11,
          display: "flex",
          alignItems: "center",
          gap: 6,
        }}
      >
        <RefreshCw size={11} /> Polled every 30s · click to expand a sidecar (live
        impl will open a logs/restart drawer)
      </div>
    </MonitorCard>
  );
}

// ---------------------------------------------------------------------------
// Proposal 2 — Detailed grid (one mini-card per sidecar)
// ---------------------------------------------------------------------------
function MiniBar({ pct, color }: { pct: number; color: string }) {
  return (
    <div
      style={{
        height: 4,
        background: "rgba(255,255,255,0.06)",
        borderRadius: 4,
        overflow: "hidden",
      }}
    >
      <div
        style={{
          width: `${Math.max(2, Math.min(100, pct))}%`,
          height: "100%",
          background: color,
          transition: "width 200ms ease-out",
        }}
      />
    </div>
  );
}

function SidecarMiniCard({ s }: { s: SidecarSnapshot }) {
  const cpuColor =
    s.cpuPct > 80 ? "var(--danger)" : s.cpuPct > 50 ? "var(--warning)" : "var(--accent)";
  const memColor =
    s.memPct > 80 ? "var(--danger)" : s.memPct > 50 ? "var(--warning)" : "var(--accent)";

  return (
    <div
      style={{
        padding: 12,
        borderRadius: 10,
        background: "var(--bg-tertiary)",
        border: "1px solid var(--border-weak)",
        display: "flex",
        flexDirection: "column",
        gap: 10,
        position: "relative",
      }}
    >
      {/* Header row */}
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <StatusDot health={s.health} size={9} />
        <span style={{ color: "var(--text-faint)", display: "flex" }}>{s.icon}</span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 13, fontWeight: 600, lineHeight: 1.2 }}>{s.name}</div>
          <div
            style={{
              fontSize: 10,
              color: "var(--text-muted)",
              whiteSpace: "nowrap",
              overflow: "hidden",
              textOverflow: "ellipsis",
            }}
          >
            {s.role}
          </div>
        </div>
        <span
          className="gt"
          style={{
            fontSize: 9,
            background:
              s.health === "ok"
                ? "rgba(106,214,163,0.12)"
                : s.health === "degraded"
                  ? "rgba(240,198,116,0.12)"
                  : "rgba(224,123,138,0.12)",
            color: HEALTH_COLOR[s.health],
            padding: "2px 6px",
            borderRadius: 999,
          }}
        >
          {HEALTH_LABEL[s.health]}
        </span>
      </div>

      {/* Metrics */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
        <div>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              fontSize: 10,
              color: "var(--text-muted)",
              marginBottom: 4,
            }}
          >
            <span>
              <Cpu size={9} style={{ verticalAlign: -1, marginRight: 3 }} />
              CPU
            </span>
            <span style={{ color: "var(--text-primary)" }}>{s.cpuPct}%</span>
          </div>
          <MiniBar pct={s.cpuPct} color={cpuColor} />
        </div>
        <div>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              fontSize: 10,
              color: "var(--text-muted)",
              marginBottom: 4,
            }}
          >
            <span>
              <HardDrive size={9} style={{ verticalAlign: -1, marginRight: 3 }} />
              MEM
            </span>
            <span style={{ color: "var(--text-primary)" }}>{s.memMiB} MiB</span>
          </div>
          <MiniBar pct={s.memPct} color={memColor} />
        </div>
      </div>

      {/* Footer row */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          fontSize: 10,
          color: "var(--text-faint)",
        }}
      >
        <span>{s.port}</span>
        <span>
          {s.restarts > 0 && (
            <span style={{ color: "var(--warning)", marginRight: 8 }}>
              <RefreshCw size={9} style={{ verticalAlign: -1, marginRight: 2 }} />
              {s.restarts}
            </span>
          )}
          uptime {formatUptime(s.uptimeMin)}
        </span>
      </div>

      {/* Hover affordance hints */}
      <div
        style={{
          display: "flex",
          gap: 6,
          fontSize: 10,
          color: "var(--text-faint)",
          paddingTop: 4,
          borderTop: "1px dashed var(--border-weak)",
        }}
      >
        <button
          type="button"
          className="glass-button"
          style={{ fontSize: 10, padding: "2px 6px" }}
          title="Tail recent logs"
        >
          <FileText size={10} /> Logs
        </button>
        <button
          type="button"
          className="glass-button"
          style={{ fontSize: 10, padding: "2px 6px" }}
          title="Restart this sidecar (real impl: revision restart)"
        >
          <Zap size={10} /> Restart
        </button>
      </div>
    </div>
  );
}

function ProposalDetailedGrid({ snapshots }: { snapshots: SidecarSnapshot[] }) {
  return (
    <MonitorCard
      title="Container App sidecars"
      subtitle="ca-elb-control · revision r0042 · 1 replica"
      status={rollupStatus(snapshots)}
      lastRefreshed={new Date()}
      onRefresh={() => {}}
      accentColor="terminal"
      collapsible
      rightSlot={
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <span className="muted" style={{ fontSize: 11 }}>
            {summary(snapshots)}
          </span>
          <a
            href="#"
            className="glass-button"
            style={{
              fontSize: 10,
              padding: "3px 8px",
              textDecoration: "none",
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
            }}
          >
            <ExternalLink size={10} /> Azure Portal
          </a>
        </div>
      }
    >
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))",
          gap: 10,
        }}
      >
        {snapshots.map((s) => (
          <SidecarMiniCard key={s.id} s={s} />
        ))}
      </div>
    </MonitorCard>
  );
}

// ---------------------------------------------------------------------------
// Proposal 3 — Topology / data-flow view
// ---------------------------------------------------------------------------
function TopoNode({
  s,
  width = 160,
}: {
  s: SidecarSnapshot;
  width?: number;
}) {
  return (
    <div
      style={{
        width,
        padding: "10px 12px",
        borderRadius: 12,
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
        <span style={{ color: "var(--text-faint)", display: "flex" }}>{s.icon}</span>
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
        <span>cpu {s.cpuPct}%</span>
        <span>mem {s.memPct}%</span>
      </div>
    </div>
  );
}

function TopoArrow({
  degraded = false,
  animated = true,
  delaySec = 0,
}: {
  degraded?: boolean;
  animated?: boolean;
  delaySec?: number;
}) {
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
      {!degraded && animated && (
        <span
          className="topo-arrow-pulse"
          aria-hidden
          style={{
            position: "absolute",
            top: -3,
            left: 0,
            width: 8,
            height: 8,
            borderRadius: 999,
            background: "var(--accent)",
            boxShadow: "0 0 12px 2px rgba(122,167,255,0.55)",
            animationDelay: `${delaySec}s`,
          }}
        />
      )}
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

function ProposalTopology({ snapshots }: { snapshots: SidecarSnapshot[] }) {
  const get = (id: string) => snapshots.find((s) => s.id === id)!;
  const fe = get("frontend");
  const api = get("api");
  const worker = get("worker");
  const beat = get("beat");
  const redis = get("redis");
  const terminal = get("terminal");

  // 5-column grid: label · left-arrow · node-left · right-arrow · node-right
  // The left/right node columns are fixed-width so frontend / redis / beat all
  // share the same left edge, and api / worker / terminal share the right edge.
  const NODE_W = 168;
  const gridStyle: React.CSSProperties = {
    display: "grid",
    gridTemplateColumns: `90px minmax(40px, 1fr) ${NODE_W}px minmax(40px, 1fr) ${NODE_W}px`,
    alignItems: "center",
    columnGap: 8,
    padding: "8px 4px",
  };
  const labelStyle: React.CSSProperties = {
    fontSize: 10,
    color: "var(--text-faint)",
    textAlign: "right",
  };
  const midLabelStyle: React.CSSProperties = {
    fontSize: 10,
    color: "var(--text-faint)",
    fontStyle: "italic",
    whiteSpace: "nowrap",
    padding: "0 6px",
  };

  return (
    <MonitorCard
      title="Container App sidecars"
      subtitle="Data flow inside ca-elb-control · revision r0042"
      status={rollupStatus(snapshots)}
      lastRefreshed={new Date()}
      onRefresh={() => {}}
      accentColor="terminal"
      collapsible
      rightSlot={
        <span className="muted" style={{ fontSize: 11 }}>
          {summary(snapshots)}
        </span>
      }
    >
      {/* Inject keyframes once for traffic animation. Defining via <style> here
          so the page is self-contained for design review. When this graduates
          into production, move the @keyframes into web/src/theme/glass.css. */}
      <style>{`
        @keyframes topoArrowPulse {
          0%   { left: 0%;   opacity: 0; }
          15%  { opacity: 1; }
          85%  { opacity: 1; }
          100% { left: calc(100% - 8px); opacity: 0; }
        }
        .topo-arrow-pulse {
          animation: topoArrowPulse 2.4s linear infinite;
        }
        @media (prefers-reduced-motion: reduce) {
          .topo-arrow-pulse { animation: none; opacity: 0.6; }
        }
      `}</style>

      {/* Top row: HTTP path — application-level flow (browser loads SPA from
          frontend, SPA calls api). The actual public ingress is on api:8080
          and api reverse-proxies static assets to frontend; that fact is
          surfaced as a small caption below the row. */}
      <div style={gridStyle}>
        <div style={labelStyle}>Browser ↣</div>
        <TopoArrow delaySec={0} />
        <TopoNode s={fe} width={NODE_W} />
        <TopoArrow delaySec={0.6} />
        <TopoNode s={api} width={NODE_W} />
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

      {/* Mid row: async path (SPA enqueue → redis broker → worker) */}
      <div style={gridStyle}>
        <div style={labelStyle}>Async tasks</div>
        <TopoArrow degraded={worker.health !== "ok" || redis.health !== "ok"} delaySec={0.3} />
        <TopoNode s={redis} width={NODE_W} />
        <TopoArrow degraded={worker.health !== "ok"} delaySec={0.9} />
        <TopoNode s={worker} width={NODE_W} />
      </div>

      {/* Bottom row: scheduler + terminal — beat anchored at the same left
          column as frontend / redis; ws/exec caption pushed into the wide
          mid-arrow so terminal aligns with api / worker on the right. */}
      <div style={gridStyle}>
        <div style={labelStyle}>Scheduled</div>
        <TopoArrow delaySec={1.2} />
        <TopoNode s={beat} width={NODE_W} />
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            minWidth: 0,
          }}
        >
          <TopoArrow degraded={terminal.health !== "ok"} delaySec={1.5} />
          <span style={midLabelStyle}>ws / exec ↣</span>
        </div>
        <TopoNode s={terminal} width={NODE_W} />
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

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------
export function SidecarDesignPreview() {
  const [variantA, setVariantA] = useState<SidecarHealth>("ok");
  // Toggle a "downgrade scenario" so reviewers can see how each design
  // reacts when sidecars start failing.
  const snapshots = MOCK_SIDECARS.map((s) =>
    variantA === "ok" ? s : { ...s, health: s.id === "redis" ? ("down" as const) : s.health },
  );

  return (
    <div className="page-stack" style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <header className="page-header" style={{ marginBottom: 0 }}>
        <div className="page-header__title">
          <Activity size={18} strokeWidth={1.5} style={{ marginRight: 8 }} />
          Sidecar status — design proposals
        </div>
        <div className="page-header__desc">
          Three visual approaches for adding a Container App sidecars panel to the
          dashboard. All three use the same mock data (one degraded sidecar, one down)
          so the comparison is apples-to-apples.{" "}
          <Link to="/" style={{ color: "var(--accent)" }}>
            ← Back to Dashboard
          </Link>
        </div>
        <div style={{ marginTop: 8 }}>
          <button
            className="glass-button"
            onClick={() => setVariantA(variantA === "ok" ? "down" : "ok")}
            style={{ fontSize: 11 }}
          >
            <AlertTriangle size={12} /> Toggle "redis goes down" scenario
          </button>
        </div>
      </header>

      <ProposalLabel
        index={1}
        title="Compact strip"
        rationale="Smallest footprint — fits in the existing dashboard grid alongside Cluster / Storage / ACR / Terminal cards. One row per sidecar with status dot, name, uptime. Click expands a logs/restart drawer (TBD)."
      />
      <ProposalCompactStrip snapshots={snapshots} />

      <ProposalLabel
        index={2}
        title="Detailed grid"
        rationale="Full-width row of mini-cards. Each card shows CPU%, memory% (bar), restart count, port, and inline Logs/Restart actions. Best when sidecars are the primary thing the operator is monitoring."
      />
      <ProposalDetailedGrid snapshots={snapshots} />

      <ProposalLabel
        index={3}
        title="Topology / data-flow"
        rationale="Visualises how requests move through the sidecars (browser → frontend → api, async path through redis → worker, scheduled by beat, terminal as a side branch). Connections highlight in amber when a path is degraded — fastest way to spot why something is broken."
      />
      <ProposalTopology snapshots={snapshots} />
    </div>
  );
}

function ProposalLabel({
  index,
  title,
  rationale,
}: {
  index: number;
  title: string;
  rationale: string;
}) {
  return (
    <div
      style={{
        padding: "8px 12px",
        borderRadius: 8,
        background: "rgba(122,167,255,0.05)",
        border: "1px solid rgba(122,167,255,0.18)",
      }}
    >
      <div style={{ fontSize: 13, fontWeight: 600 }}>
        Proposal {index} — {title}
      </div>
      <div className="muted" style={{ fontSize: 11, marginTop: 2 }}>
        {rationale}
      </div>
    </div>
  );
}

export default SidecarDesignPreview;
