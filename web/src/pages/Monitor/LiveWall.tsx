/**
 * LiveWall — Monitor / Live Wall page.
 *
 * 2×3 grid of sidecar tiles, each showing health + sparkline + live log tail.
 * Reuses the existing useSidecarMetrics SSE for metrics so the wall shares
 * one connection across all six tiles; each tile owns its own log SSE.
 *
 * MVP scope (Phase 1):
 *   - 6 fixed sidecars (frontend, api, worker, beat, redis, terminal)
 *   - global filter (regex) applied to every tile
 *   - global pause toggle
 *   - source/connection indicator in the page header
 *
 * Deferred (Phase 2 / 3): expand-to-full-panel inline, Inspector deep-link,
 * Timeline view.
 */
import { useMemo, useState } from "react";
import { Pause, Play } from "lucide-react";

import { SIDECAR_CONTAINERS, type SidecarContainer } from "@/api/sidecarLogs";
import { useSidecarMetrics } from "@/hooks/useSidecarMetrics";

import { SidecarLiveTile } from "./SidecarLiveTile";
import "./LiveWall.css";

export function LiveWall() {
  const metrics = useSidecarMetrics();
  const [filter, setFilter] = useState("");
  const [paused, setPaused] = useState(false);

  const sidecars = useMemo(
    () => metrics.data?.sidecars ?? {},
    [metrics.data?.sidecars],
  );
  const summary = useMemo(() => {
    const items = SIDECAR_CONTAINERS.map((c) => sidecars[c]);
    const healthy = items.filter((m) => m?.health === "ok").length;
    const degraded = items.filter((m) => m?.health === "degraded").length;
    const down = items.filter((m) => m?.health === "down").length;
    const unknown = items.filter((m) => m == null).length;
    return { healthy, degraded, down, unknown };
  }, [sidecars]);

  const revision = metrics.data?.revision ?? "—";
  const connectionLabel =
    metrics.source === "live" ? "live" :
    metrics.source === "polling" ? "polling" :
    "connecting";

  return (
    <div className="live-wall">
      <header className="live-wall__header">
        <div className="live-wall__title-row">
          <h1 className="live-wall__title">Live Wall</h1>
          <p className="live-wall__lede">
            All six Container App sidecars at a glance. Each tile streams logs in
            real time and surfaces CPU / memory pressure as a sparkline.
          </p>
        </div>

        <div className="live-wall__chips">
          <span className="live-wall__chip live-wall__chip--meta">
            ca-elb-dashboard · revision <code>{revision}</code>
          </span>
          <span className="live-wall__chip live-wall__chip--ok">
            {summary.healthy} healthy
          </span>
          {summary.degraded > 0 && (
            <span className="live-wall__chip live-wall__chip--warn">
              {summary.degraded} degraded
            </span>
          )}
          {summary.down > 0 && (
            <span className="live-wall__chip live-wall__chip--danger">
              {summary.down} down
            </span>
          )}
          {summary.unknown > 0 && (
            <span className="live-wall__chip live-wall__chip--muted">
              {summary.unknown} unknown
            </span>
          )}
          <span className={`live-wall__source live-wall__source--${metrics.source}`}>
            metrics: {connectionLabel}
          </span>
        </div>
      </header>

      <div className="live-wall__toolbar">
        <input
          type="text"
          className="live-wall__filter"
          placeholder='filter all tiles  (regex, e.g.  "job-218|ERROR")'
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          aria-label="Filter log lines across all tiles"
        />
        <button
          type="button"
          className="live-wall__toggle"
          onClick={() => setPaused((p) => !p)}
          aria-pressed={paused}
        >
          {paused ? <Play size={14} strokeWidth={1.8} /> : <Pause size={14} strokeWidth={1.8} />}
          {paused ? "Resume all" : "Pause all"}
        </button>
      </div>

      <section className="live-wall__grid" aria-label="Sidecars">
        {SIDECAR_CONTAINERS.map((container: SidecarContainer) => (
          <SidecarLiveTile
            key={container}
            container={container}
            metric={sidecars[container]}
            globalFilter={filter}
            globalPaused={paused}
          />
        ))}
      </section>

      <p className="live-wall__footnote">
        Logs are sanitised server-side (bearer tokens, SAS signatures, and
        Authorization headers redacted) before they reach the browser. Each
        tile keeps the last 60 lines in memory; older lines are dropped.
      </p>
    </div>
  );
}

export default LiveWall;
