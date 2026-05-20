import type { CSSProperties } from "react";
import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { Activity, X } from "lucide-react";

import { MonitorCard } from "@/components/MonitorCard";
import {
  useSidecarMetrics,
  type SidecarMetric,
} from "@/hooks/useSidecarMetrics";

import { NODE_W, PARTICLES_PER_TICK_CAP, PLACEHOLDER } from "./constants";
import { HttpInspectorPanel } from "./HttpInspectorPanel";
import { rollupStatus, staleSnapshot, summary } from "./helpers";
import { NearRealtimeLabel } from "./NearRealtimeLabel";
import { RowParticle } from "./RowParticle";
import { StatusDot } from "./StatusDot";
import { TOPO_ROW_PARTICLE_CSS } from "./topoRowParticleCss";
import { TopoArrow } from "./TopoArrow";
import { TopoNode } from "./TopoNode";
import { useEventParticles } from "./useEventParticles";

/**
 * Control Plane Sidecars card — topology view of the in-revision sidecars
 * (frontend, api, worker, beat, redis, terminal) with near-real-time
 * CPU/MEM and an animated traffic pulse along each healthy data path.
 *
 * Data source: useSidecarMetrics() — SSE-pushed snapshots every 5 s via
 * /api/monitor/sidecars/events, with 30 s polling fallback to
 * /api/monitor/sidecars on connection loss.
 */
export function SidecarsCard() {
  const { data, source, lastUpdated, isError, isStale } = useSidecarMetrics();
  const displayData = isStale ? staleSnapshot(data) : data;
  const sidecars = displayData?.sidecars ?? {};
  const loaded = (id: string): boolean => Boolean(sidecars[id]);
  const get = (id: string): SidecarMetric =>
    sidecars[id] ?? { ...PLACEHOLDER, name: id };
  const [inspectorOpen, setInspectorOpen] = useState(false);

  useEffect(() => {
    if (!inspectorOpen) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setInspectorOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKey);
    };
  }, [inspectorOpen]);

  const fe = get("frontend");
  const api = get("api");
  const worker = get("worker");
  const beat = get("beat");
  const redis = get("redis");
  const terminal = get("terminal");
  const feLoaded = loaded("frontend");
  const apiLoaded = loaded("api");
  const workerLoaded = loaded("worker");
  const beatLoaded = loaded("beat");
  const redisLoaded = loaded("redis");
  const terminalLoaded = loaded("terminal");

  const {
    particles,
    lastCounts,
    remove: removeParticle,
  } = useEventParticles(displayData);
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

  const gridStyle: CSSProperties = {
    display: "grid",
    gridTemplateColumns: `90px minmax(40px, 1fr) ${NODE_W}px minmax(40px, 1fr) ${NODE_W}px`,
    alignItems: "center",
    columnGap: 8,
    padding: "8px 4px",
    position: "relative",
  };
  const labelStyle: CSSProperties = {
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
          <button
            type="button"
            className="glass-button"
            onClick={() => setInspectorOpen(true)}
            aria-pressed={inspectorOpen}
            aria-expanded={inspectorOpen}
            aria-controls="sidecar-http-inspector-modal"
            title={
              inspectorOpen
                ? "HTTP request inspector is open"
                : "Inspect every HTTP request flowing through the api sidecar"
            }
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
              fontSize: 10,
              padding: "3px 8px",
              color: inspectorOpen ? "var(--accent)" : undefined,
              borderColor: inspectorOpen ? "var(--accent)" : undefined,
            }}
          >
            <Activity size={11} />
            Inspect HTTP requests
          </button>
        </div>
      }
    >
      <style>{TOPO_ROW_PARTICLE_CSS}</style>

      {/* Row 1: HTTP path */}
      <div style={gridStyle}>
        <div style={labelStyle}>
          Browser ↣{rowBadge(1)}
        </div>
        <TopoArrow />
        <TopoNode s={fe} loading={!feLoaded} />
        <TopoArrow />
        <TopoNode s={api} loading={!apiLoaded} />
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
        <TopoArrow degraded={(workerLoaded && worker.health !== "ok") || (redisLoaded && redis.health !== "ok")} />
        <TopoNode s={redis} loading={!redisLoaded} />
        <TopoArrow degraded={workerLoaded && worker.health !== "ok"} />
        <TopoNode s={worker} loading={!workerLoaded} />
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
        <TopoNode s={beat} loading={!beatLoaded} />
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
        <TopoNode s={api} loading={!apiLoaded} />
        <TopoArrow degraded={terminalLoaded && terminal.health !== "ok"} />
        <TopoNode s={terminal} loading={!terminalLoaded} />
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
        tools (<code>azcopy</code>, <code>kubectl</code>,{" "}
        <code>elastic-blast</code>).
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
        <span style={{ color: "var(--accent)" }}>
          ● dot = real event since last tick
        </span>
      </div>

      {inspectorOpen &&
        createPortal(
        <div
          id="sidecar-http-inspector-modal"
          role="dialog"
          aria-modal="true"
          aria-label="HTTP request inspector"
          style={{
            position: "fixed",
            inset: 0,
            zIndex: 10000,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            padding: "24px",
            background: "rgba(3, 6, 14, 0.72)",
            backdropFilter: "blur(10px)",
          }}
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) setInspectorOpen(false);
          }}
        >
          <div
            className="glass-card glass-card--strong"
            style={{
              width: "min(1280px, 96vw)",
              maxHeight: "92vh",
              overflow: "hidden",
              display: "flex",
              flexDirection: "column",
              padding: 16,
              borderRadius: 10,
              boxShadow: "0 18px 60px rgba(0,0,0,0.48)",
            }}
          >
            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: 12,
                marginBottom: 10,
              }}
            >
              <div>
                <div
                  style={{
                    fontSize: 10,
                    textTransform: "uppercase",
                    letterSpacing: "0.08em",
                    color: "var(--accent)",
                    fontWeight: 700,
                  }}
                >
                  API sidecar
                </div>
                <div style={{ fontSize: 16, fontWeight: 700 }}>
                  HTTP request inspector
                </div>
              </div>
              <button
                type="button"
                className="glass-button"
                onClick={() => setInspectorOpen(false)}
                aria-label="Close HTTP request inspector"
                title="Close (Esc)"
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  justifyContent: "center",
                  width: 30,
                  height: 30,
                  padding: 0,
                }}
              >
                <X size={15} />
              </button>
            </div>
            <div style={{ overflow: "auto", paddingRight: 2 }}>
              <HttpInspectorPanel />
            </div>
          </div>
        </div>,
        document.body,
      )}
    </MonitorCard>
  );
}

export default SidecarsCard;
