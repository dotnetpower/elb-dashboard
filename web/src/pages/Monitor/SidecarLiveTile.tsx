/**
 * SidecarLiveTile — one cell of the Live Wall grid.
 *
 * Renders, for a single sidecar:
 *   • name + role + health pill (from useSidecarMetrics snapshot)
 *   • CPU / MEM sparkline (last ~5 min, accumulated client-side)
 *   • last N live log lines (from useSidecarLogs)
 *   • footer with alert count + "Open in Inspector ↗" deep-link
 *
 * The tile gets its metric snapshot *from the parent* so the whole wall
 * shares one SSE connection instead of opening six.
 */
import { useEffect, useMemo, useRef, useState } from "react";

import type { SidecarContainer } from "@/api/sidecarLogs";
import type { SidecarMetric } from "@/hooks/useSidecarMetrics";
import { useSidecarLogs } from "@/hooks/useSidecarLogs";
import { ChevronRight, Maximize2, Pause, Play } from "lucide-react";

import { SidecarLogModal, type LevelFilter } from "./SidecarLogModal";

const SIDECAR_ROLES: Record<SidecarContainer, string> = {
  frontend: "nginx · :8081",
  api: "FastAPI · :8080",
  worker: "celery worker",
  beat: "celery beat",
  redis: "broker · :6379",
  terminal: "ttyd · exec :7682",
};

interface SidecarLiveTileProps {
  container: SidecarContainer;
  metric: SidecarMetric | undefined;
  globalFilter?: string;
  globalPaused?: boolean;
}

const SPARK_CAPACITY = 60;
const VISIBLE_LOG_LINES = 6;

export function SidecarLiveTile({ container, metric, globalFilter, globalPaused }: SidecarLiveTileProps) {
  const [paused, setPaused] = useState(false);
  const [expanded, setExpanded] = useState<LevelFilter | null>(null);
  const effectivePaused = paused || Boolean(globalPaused);

  const { lines, source, dropped } = useSidecarLogs(container, { paused: effectivePaused });

  // ---- sparkline history (client-side, per-tile) ----
  const cpuHist = useMetricHistory(metric?.cpu_pct);
  const memPct = metric?.mem_pct ?? (metric?.mem_bytes && metric?.mem_max_bytes
    ? (metric.mem_bytes / metric.mem_max_bytes) * 100
    : null);
  const memHist = useMetricHistory(memPct ?? undefined);

  // ---- filter + level summary ----
  const filterRegex = useMemo(() => {
    if (!globalFilter || globalFilter.trim() === "") return null;
    try {
      return new RegExp(globalFilter, "i");
    } catch {
      return null;
    }
  }, [globalFilter]);

  const visible = useMemo(() => {
    const arr = filterRegex
      ? lines.filter((l) => filterRegex.test(l.text))
      : lines;
    return arr.slice(Math.max(0, arr.length - VISIBLE_LOG_LINES));
  }, [lines, filterRegex]);

  const errCount = useMemo(() => lines.filter((l) => l.level === "ERR").length, [lines]);
  const warnCount = useMemo(() => lines.filter((l) => l.level === "WARN").length, [lines]);

  // ---- styling derived from health + alerts ----
  const health = metric?.health ?? "down";
  const tileClass = [
    "live-tile",
    errCount > 0 || health === "down" ? "live-tile--alert" : "",
    errCount === 0 && warnCount > 0 ? "live-tile--warn" : "",
  ].filter(Boolean).join(" ");

  const memBytesText = formatBytes(metric?.mem_bytes);
  const memMaxText = formatBytes(metric?.mem_max_bytes);

  return (
    <article className={tileClass}>
      <header className="live-tile__head">
        <span className={`live-tile__dot live-tile__dot--${health}`} aria-hidden="true" />
        <span className="live-tile__name">{container}</span>
        <span className="live-tile__role">{SIDECAR_ROLES[container]}</span>
        {errCount > 0 && (
          <button
            type="button"
            className="live-tile__pill live-tile__pill--danger live-tile__pill--button"
            onClick={() => setExpanded("ERR")}
            title={`Inspect the ${errCount} error line${errCount === 1 ? "" : "s"}`}
          >
            {errCount} ERR
          </button>
        )}
        {errCount === 0 && warnCount > 0 && (
          <button
            type="button"
            className="live-tile__pill live-tile__pill--warning live-tile__pill--button"
            onClick={() => setExpanded("WARN")}
            title={`Inspect the ${warnCount} warning line${warnCount === 1 ? "" : "s"}`}
          >
            {warnCount} WARN
          </button>
        )}
        <div className="live-tile__actions">
          <button
            type="button"
            className="live-tile__icon-btn"
            onClick={() => setPaused((p) => !p)}
            title={effectivePaused ? "Resume" : "Pause"}
            aria-label={effectivePaused ? "Resume" : "Pause"}
          >
            {effectivePaused ? <Play size={12} strokeWidth={2} /> : <Pause size={12} strokeWidth={2} />}
          </button>
          <button
            type="button"
            className="live-tile__icon-btn"
            onClick={() => setExpanded("ALL")}
            title="Expand logs"
            aria-label="Expand logs"
          >
            <Maximize2 size={12} strokeWidth={2} />
          </button>
        </div>
      </header>

      <div className="live-tile__sparks">
        <SparkCell
          label="CPU"
          valueText={metric?.cpu_pct != null ? `${metric.cpu_pct.toFixed(0)}%` : "—"}
          history={cpuHist}
          variant={cpuHist.at(-1) != null && cpuHist.at(-1)! > 80 ? "danger" : cpuHist.at(-1) != null && cpuHist.at(-1)! > 60 ? "warn" : "ok"}
        />
        <SparkCell
          label="MEM"
          valueText={memBytesText && memMaxText ? `${memBytesText} / ${memMaxText}` : memBytesText ?? "—"}
          history={memHist}
          variant={memPct != null && memPct > 85 ? "danger" : memPct != null && memPct > 70 ? "warn" : "ok"}
        />
      </div>

      <div className="live-tile__log" role="log" aria-live="polite" aria-label={`${container} log tail`}>
        {visible.length === 0 ? (
          <p className="live-tile__log-empty">
            {source === "connecting" ? "connecting…" : "no recent activity"}
          </p>
        ) : (
          visible.map((line, idx) => (
            <div key={`${line.ts}-${idx}`} className="live-tile__log-line">
              <span className="live-tile__log-ts">{formatHm(line.ts)}</span>
              <span className={`live-tile__log-lvl live-tile__log-lvl--${line.level ?? "INFO"}`}>
                {line.level ?? "INFO"}
              </span>
              <span className="live-tile__log-msg">{line.text}</span>
            </div>
          ))
        )}
      </div>

      <footer className="live-tile__foot">
        <span className="live-tile__foot-stats">
          <span className={`live-tile__source live-tile__source--${source}`}>{sourceLabel(source)}</span>
          {dropped > 0 && <span className="live-tile__dropped">{dropped} dropped</span>}
        </span>
        <button
          type="button"
          className="live-tile__inspect live-tile__inspect--active"
          onClick={() => setExpanded("ALL")}
          title="Open the full log view"
        >
          Full logs <ChevronRight size={12} strokeWidth={2} />
        </button>
      </footer>

      {expanded && (
        <SidecarLogModal
          container={container}
          role={SIDECAR_ROLES[container]}
          liveLines={lines}
          initialLevel={expanded}
          onClose={() => setExpanded(null)}
        />
      )}
    </article>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function useMetricHistory(current: number | undefined): number[] {
  const [series, setSeries] = useState<number[]>([]);
  const lastRef = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (current == null || Number.isNaN(current)) return;
    if (lastRef.current === current) return;
    lastRef.current = current;
    setSeries((prev) => {
      const next = prev.concat(current);
      return next.length > SPARK_CAPACITY ? next.slice(next.length - SPARK_CAPACITY) : next;
    });
  }, [current]);
  return series;
}

interface SparkCellProps {
  label: string;
  valueText: string;
  history: number[];
  variant: "ok" | "warn" | "danger";
}

function SparkCell({ label, valueText, history, variant }: SparkCellProps) {
  const path = buildSparkPath(history, 120, 22);
  return (
    <div className="live-tile__spark">
      <div className="live-tile__spark-row">
        <span className="live-tile__spark-lbl">{label}</span>
        <span className="live-tile__spark-val">{valueText}</span>
      </div>
      <svg
        className={`live-tile__spark-svg live-tile__spark-svg--${variant}`}
        viewBox="0 0 120 22"
        preserveAspectRatio="none"
        aria-hidden="true"
      >
        <path className="live-tile__spark-area" d={path.area} />
        <path className="live-tile__spark-line" d={path.line} />
      </svg>
    </div>
  );
}

function buildSparkPath(series: number[], w: number, h: number): { line: string; area: string } {
  if (series.length === 0) {
    return { line: `M0 ${h - 1} L${w} ${h - 1}`, area: `M0 ${h - 1} L${w} ${h - 1} L${w} ${h} L0 ${h} Z` };
  }
  const max = Math.max(100, ...series);
  const step = series.length > 1 ? w / (series.length - 1) : w;
  const points = series.map((v, i) => {
    const x = i * step;
    const y = h - 1 - (Math.max(0, Math.min(max, v)) / max) * (h - 2);
    return [x, y] as const;
  });
  const line = points.map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(1)} ${y.toFixed(1)}`).join(" ");
  const first = points[0];
  const last = points[points.length - 1];
  const area = `${line} L${last[0].toFixed(1)} ${h} L${first[0].toFixed(1)} ${h} Z`;
  return { line, area };
}

function formatBytes(b: number | null | undefined): string | null {
  if (b == null) return null;
  if (b < 1024) return `${b}B`;
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(0)}K`;
  if (b < 1024 * 1024 * 1024) return `${(b / 1024 / 1024).toFixed(0)}M`;
  return `${(b / 1024 / 1024 / 1024).toFixed(1)}G`;
}

function formatHm(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "--:--";
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function sourceLabel(s: "connecting" | "live" | "polling" | "mock"): string {
  switch (s) {
    case "live": return "● live";
    case "polling": return "polling";
    case "mock": return "mock";
    case "connecting":
    default:
      return "connecting…";
  }
}
