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
import { useCallback, useEffect, useRef, useState } from "react";

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

function staleSnapshot(snap: SidecarsSnapshot | undefined): SidecarsSnapshot | undefined {
  if (!snap) return snap;
  return {
    ...snap,
    degraded: true,
    degraded_reason: "sidecar snapshot is stale",
    sidecars: Object.fromEntries(
      Object.entries(snap.sidecars).map(([key, sidecar]) => [
        key,
        {
          ...sidecar,
          health: "degraded" as const,
          cpu_pct: undefined,
          mem_pct: undefined,
        },
      ]),
    ),
  };
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
  onEnd,
}: {
  delaySec?: number;
  durationSec?: number;
  endRight?: string;
  onEnd?: () => void;
}) {
  const style: React.CSSProperties & Record<string, string> = {
    animationDelay: `${delaySec}s`,
    // One-shot: each particle represents a single real event drained from
    // the snapshot. Looping would re-introduce the decorative behaviour we
    // just removed. The class default (set in the <style> block below) is
    // overridden by this inline value.
    animationIterationCount: "1",
    animationFillMode: "forwards",
  };
  if (endRight) style["--row-end"] = endRight;
  if (durationSec) style.animationDuration = `${durationSec}s`;
  return (
    <span
      className="topo-row-particle"
      aria-hidden
      style={style}
      onAnimationEnd={onEnd}
    />
  );
}

// ---------------------------------------------------------------------------
// Event-driven particle queue
// ---------------------------------------------------------------------------
interface ParticleEvent {
  id: number;
  row: 1 | 2 | 3 | 4;
  delaySec: number;
}

// Cap per-row particles per snapshot so a sudden burst (e.g. dashboard mount
// firing 6+ ARM/monitor calls in one second) doesn't render a wall of dots.
// The count is also reflected in the row badge so users see the real number.
const PARTICLES_PER_TICK_CAP = 6;
// Stagger so a multi-event tick reads as a stream rather than overlapping dots.
const PARTICLE_STAGGER_SEC = 0.18;
// Hard upper bound on the queue. onAnimationEnd is the *primary* removal
// path, but it is not guaranteed: CSS `animation: none` (reduced motion),
// `display: none` (collapsed/hidden card), background-tab throttling, or
// a render bug can all stop the event from firing. The hard cap prevents
// unbounded growth in those cases — see the safety timeout below.
const PARTICLE_QUEUE_HARD_CAP = 64;
// Time budget for a single particle: base CSS duration (1.6 s) + max stagger
// (PARTICLES_PER_TICK_CAP * PARTICLE_STAGGER_SEC) + headroom. After this we
// force-remove regardless of whether onAnimationEnd fired.
const PARTICLE_LIFETIME_MS =
  Math.ceil((1.6 + PARTICLES_PER_TICK_CAP * PARTICLE_STAGGER_SEC + 0.6) * 1000);

function useReducedMotion(): boolean {
  const get = () => {
    if (typeof window === "undefined" || !window.matchMedia) return false;
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  };
  const [reduced, setReduced] = useState(get);
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const onChange = () => setReduced(mq.matches);
    mq.addEventListener?.("change", onChange);
    return () => mq.removeEventListener?.("change", onChange);
  }, []);
  return reduced;
}

function usePageVisible(): boolean {
  const get = () =>
    typeof document === "undefined" ? true : document.visibilityState === "visible";
  const [visible, setVisible] = useState(get);
  useEffect(() => {
    if (typeof document === "undefined") return;
    const onChange = () => setVisible(document.visibilityState === "visible");
    document.addEventListener("visibilitychange", onChange);
    return () => document.removeEventListener("visibilitychange", onChange);
  }, []);
  return visible;
}

function useEventParticles(data: SidecarsSnapshot | undefined): {
  particles: ParticleEvent[];
  lastCounts: Record<"row1" | "row2" | "row3" | "row4", number>;
  remove: (id: number) => void;
} {
  const reducedMotion = useReducedMotion();
  const pageVisible = usePageVisible();
  const [particles, setParticles] = useState<ParticleEvent[]>([]);
  const [lastCounts, setLastCounts] = useState<
    Record<"row1" | "row2" | "row3" | "row4", number>
  >({ row1: 0, row2: 0, row3: 0, row4: 0 });
  const seenTsRef = useRef<number | null>(null);
  const idRef = useRef(1);
  const timersRef = useRef<Map<number, ReturnType<typeof setTimeout>>>(new Map());

  // Single removal path used by both onAnimationEnd and the safety timer.
  // Idempotent — the second caller is a no-op once the id is gone.
  const remove = useCallback((id: number) => {
    const timer = timersRef.current.get(id);
    if (timer) {
      clearTimeout(timer);
      timersRef.current.delete(id);
    }
    setParticles((prev) => (prev.some((p) => p.id === id) ? prev.filter((p) => p.id !== id) : prev));
  }, []);

  useEffect(() => {
    if (!data || data.ts == null) return;
    // Snapshots arrive every 5s; ts is the server-side time. De-dupe so a
    // re-render or polling fallback re-reading the same snapshot doesn't
    // double-fire particles.
    if (seenTsRef.current === data.ts) return;
    seenTsRef.current = data.ts;

    const events = data.events ?? {};
    const rawCounts: Record<"row1" | "row2" | "row3" | "row4", number> = {
      row1: Math.max(0, Math.trunc(Number(events.row1 ?? 0))),
      row2: Math.max(0, Math.trunc(Number(events.row2 ?? 0))),
      row3: Math.max(0, Math.trunc(Number(events.row3 ?? 0))),
      row4: Math.max(0, Math.trunc(Number(events.row4 ?? 0))),
    };
    // Always update the badge counts (cheap and authoritative).
    setLastCounts((prev) =>
      prev.row1 === rawCounts.row1 &&
      prev.row2 === rawCounts.row2 &&
      prev.row3 === rawCounts.row3 &&
      prev.row4 === rawCounts.row4
        ? prev
        : rawCounts,
    );

    // Skip particle DOM work when the user can't or won't see them.
    // The badge above still surfaces the count, so accuracy is preserved.
    if (reducedMotion || !pageVisible) return;

    const additions: ParticleEvent[] = [];
    (["row1", "row2", "row3", "row4"] as const).forEach((rowKey, i) => {
      const cap = Math.min(PARTICLES_PER_TICK_CAP, rawCounts[rowKey]);
      for (let j = 0; j < cap; j++) {
        additions.push({
          id: idRef.current++,
          row: (i + 1) as 1 | 2 | 3 | 4,
          delaySec: j * PARTICLE_STAGGER_SEC,
        });
      }
    });
    if (additions.length === 0) return;

    setParticles((prev) => {
      const merged = [...prev, ...additions];
      // Hard cap — drop the oldest particles (which have likely already
      // started fading) and clear their safety timers. Prevents unbounded
      // growth if onAnimationEnd never fires for some reason.
      if (merged.length <= PARTICLE_QUEUE_HARD_CAP) return merged;
      const overflow = merged.length - PARTICLE_QUEUE_HARD_CAP;
      const dropped = merged.slice(0, overflow);
      for (const d of dropped) {
        const t = timersRef.current.get(d.id);
        if (t) {
          clearTimeout(t);
          timersRef.current.delete(d.id);
        }
      }
      return merged.slice(overflow);
    });

    // Schedule a safety force-remove for each new particle.
    for (const p of additions) {
      const totalMs = PARTICLE_LIFETIME_MS + Math.ceil(p.delaySec * 1000);
      const t = setTimeout(() => remove(p.id), totalMs);
      timersRef.current.set(p.id, t);
    }
  }, [data, reducedMotion, pageVisible, remove]);

  // Component unmount: clear every pending timer so we don't queue setState
  // calls into a dead component (React would warn in dev).
  useEffect(() => {
    const timers = timersRef.current;
    return () => {
      for (const t of timers.values()) clearTimeout(t);
      timers.clear();
    };
  }, []);

  return { particles, lastCounts, remove };
}

// ---------------------------------------------------------------------------
// Card
// ---------------------------------------------------------------------------
export function SidecarsCard() {
  const { data, source, lastUpdated, isError, isStale } = useSidecarMetrics();
  const displayData = isStale ? staleSnapshot(data) : data;
  const sidecars = displayData?.sidecars ?? {};
  const get = (id: string): SidecarMetric =>
    sidecars[id] ?? { ...PLACEHOLDER, name: id };

  const fe = get("frontend");
  const api = get("api");
  const worker = get("worker");
  const beat = get("beat");
  const redis = get("redis");
  const terminal = get("terminal");

  const { particles, lastCounts, remove: removeParticle } = useEventParticles(displayData);
  const renderRowParticles = (
    row: 1 | 2 | 3 | 4,
    extra?: { durationSec?: number; endRight?: string },
  ) =>
    particles
      .filter((p) => p.row === row)
      .map((p) => (
        <RowParticle
          key={p.id}
          delaySec={p.delaySec}
          durationSec={extra?.durationSec}
          endRight={extra?.endRight}
          onEnd={() => removeParticle(p.id)}
        />
      ));

  const rowBadge = (row: 1 | 2 | 3 | 4) => {
    const count = lastCounts[`row${row}` as keyof typeof lastCounts];
    if (!count) return null;
    const overflow = count > PARTICLES_PER_TICK_CAP;
    return (
      <span
        title={`${count} event${count === 1 ? "" : "s"} since last tick${overflow ? ` (showing ${PARTICLES_PER_TICK_CAP} dots)` : ""}`}
        style={{
          marginLeft: 6,
          fontSize: 9,
          fontWeight: 600,
          padding: "1px 5px",
          borderRadius: 999,
          background: "rgba(122, 167, 255, 0.16)",
          color: "var(--accent)",
          border: "1px solid rgba(122, 167, 255, 0.32)",
        }}
      >
        {overflow ? `${PARTICLES_PER_TICK_CAP}+` : count}
      </span>
    );
  };

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
      subtitle={
        isStale
          ? `Data flow inside ca-elb-control · stale snapshot · revision ${data?.revision ?? "?"}`
          : `Data flow inside ca-elb-control · revision ${data?.revision ?? "?"}`
      }
      status={isError && !displayData ? "error" : rollupStatus(displayData)}
      lastRefreshed={lastUpdated}
      onRefresh={() => {}}
      accentColor="terminal"
      collapsible
      rightSlot={
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <NearRealtimeLabel source={source} />
          <span className="muted" style={{ fontSize: 11 }}>
            {isStale ? "snapshot stale" : summary(displayData)}
          </span>
        </div>
      }
    >
      {/* Animation keyframes — mirror of /sidecar-design-preview. Keep
          here so the card is self-contained; once the preview route is
          deleted these can move into web/src/theme/glass.css.
          The class default omits `animation-iteration-count` so each
          <RowParticle> can override it (we use 1 for event-driven dots). */}
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
          animation-name: topoRowParticle;
          animation-duration: 1.6s;
          animation-timing-function: linear;
        }
        @media (prefers-reduced-motion: reduce) {
          .topo-row-particle { animation: none; opacity: 0.6; }
        }
      `}</style>

      {/* Row 1: HTTP path */}
      <div style={gridStyle}>
        <div style={labelStyle}>
          Browser ↣{rowBadge(1)}
        </div>
        <TopoArrow />
        <TopoNode s={fe} />
        <TopoArrow />
        <TopoNode s={api} />
        {renderRowParticles(1)}
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
        <div style={labelStyle}>
          Async ↣{rowBadge(2)}
        </div>
        <TopoArrow degraded={worker.health !== "ok" || redis.health !== "ok"} />
        <TopoNode s={redis} />
        <TopoArrow degraded={worker.health !== "ok"} />
        <TopoNode s={worker} />
        {renderRowParticles(2)}
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
        <div style={labelStyle}>
          Scheduled ↣{rowBadge(3)}
        </div>
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
        {renderRowParticles(3, {
          durationSec: 0.9,
          endRight: "calc((100% - 458px) / 2 + 250px)",
        })}
      </div>

      {/* Row 4: terminal channel — api proxies WebSocket + privileged exec */}
      <div style={gridStyle}>
        <div style={labelStyle}>
          ws / exec ↣{rowBadge(4)}
        </div>
        <TopoArrow />
        <TopoNode s={api} />
        <TopoArrow degraded={terminal.health !== "ok"} />
        <TopoNode s={terminal} />
        {renderRowParticles(4)}
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
        <span style={{ color: "var(--accent)" }}>● dot = real event since last tick</span>
      </div>
    </MonitorCard>
  );
}

export default SidecarsCard;
