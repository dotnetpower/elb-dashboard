/**
 * Reusable visual atoms for the cluster bento layout.
 *
 * Originally derived from a design-exploration prototype (the `mockups`
 * pages were retired in issue #24). They are kept deliberately
 * self-contained — no contextual state, no fetches — so they remain easy
 * to reuse from other dashboard surfaces (per-job details, modal, BLAST
 * analytics).
 */

import {
  AlertTriangle,
  CheckCircle2,
  Info,
  Loader2,
  TrendingDown,
  TrendingUp,
  XCircle,
} from "lucide-react";
import { useId } from "react";
import type { CSSProperties, ReactNode } from "react";

import type { DisplayJobState, JobRowView } from "./jobTypes";
import "./ClusterBento.css";

/* -------------------------------------------------------------------------- */
/* Health pill                                                                */
/* -------------------------------------------------------------------------- */

export type ClusterHealth = "healthy" | "degraded" | "down" | "unknown" | "provisioning";

export function HealthPill({ health }: { health: ClusterHealth }) {
  const tone =
    health === "healthy"
      ? "var(--success)"
      : health === "provisioning"
        ? "var(--accent)"
      : health === "degraded"
        ? "var(--warning)"
        : health === "down"
          ? "var(--danger)"
          : "var(--text-faint)";
  const Icon =
    health === "healthy"
      ? CheckCircle2
      : health === "provisioning"
        ? Loader2
      : health === "degraded"
        ? AlertTriangle
        : health === "down"
          ? XCircle
          : Loader2;
  const label = health === "unknown" ? "Unknown" : health[0].toUpperCase() + health.slice(1);
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        padding: "4px 10px 4px 8px",
        borderRadius: 999,
        background: `${tone}1a`,
        border: `1px solid ${tone}55`,
        color: tone,
        fontSize: 11,
        fontWeight: 600,
        letterSpacing: "0.02em",
      }}
    >
      <Icon
        size={12}
        strokeWidth={2.2}
        className={health === "provisioning" ? "spin" : undefined}
      />
      {label}
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/* Trend badge — small +/− % with arrow                                       */
/* -------------------------------------------------------------------------- */

export function TrendBadge({ d }: { d: number }) {
  if (!Number.isFinite(d) || Math.abs(d) < 0.02) {
    return (
      <span
        style={{
          fontSize: 10,
          color: "var(--text-faint)",
          fontVariantNumeric: "tabular-nums",
        }}
      >
        ±0%
      </span>
    );
  }
  const up = d > 0;
  const Icon = up ? TrendingUp : TrendingDown;
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 2,
        fontSize: 10,
        fontWeight: 600,
        fontVariantNumeric: "tabular-nums",
      }}
    >
      <Icon size={10} strokeWidth={2.2} />
      {up ? "+" : ""}
      {Math.round(d * 100)}%
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/* Sparkline (Catmull-Rom -> cubic Bezier)                                    */
/* -------------------------------------------------------------------------- */

export function Spark({
  data,
  color,
  width = 120,
  height = 32,
  fill = true,
  strokeWidth = 1.5,
  smooth = true,
  ariaLabel,
}: {
  data: number[];
  color: string;
  width?: number;
  height?: number;
  fill?: boolean;
  strokeWidth?: number;
  smooth?: boolean;
  ariaLabel?: string;
}) {
  // `useId()` must run before any early return to satisfy the Rules of
  // Hooks. It yields a stable, collision-free gradient id per Spark
  // instance (replacing the old Math.random id that churned every paint).
  const reactId = useId();
  if (!data || data.length === 0) return null;
  // Strip non-finite samples — a single NaN/Infinity poisons every
  // computed coordinate (NaN propagates through min/max/range), and
  // SVG doesn't render paths that contain NaN.  Also defends against
  // upstream payloads where `count` came back as `null` or a string.
  const safeData = data.filter((v) => Number.isFinite(v)) as number[];
  if (safeData.length === 0) return null;
  const min = Math.min(...safeData);
  const max = Math.max(...safeData);
  const range = max - min || 1;
  const step = width / (safeData.length - 1 || 1);
  const pts = safeData.map(
    (v, i) => [i * step, height - ((v - min) / range) * (height - 2) - 1] as const,
  );

  let d = `M ${pts[0][0]} ${pts[0][1]}`;
  if (smooth && pts.length > 2) {
    for (let i = 0; i < pts.length - 1; i++) {
      const p0 = pts[i - 1] || pts[i];
      const p1 = pts[i];
      const p2 = pts[i + 1];
      const p3 = pts[i + 2] || p2;
      const cp1x = p1[0] + (p2[0] - p0[0]) / 6;
      const cp1y = p1[1] + (p2[1] - p0[1]) / 6;
      const cp2x = p2[0] - (p3[0] - p1[0]) / 6;
      const cp2y = p2[1] - (p3[1] - p1[1]) / 6;
      d += ` C ${cp1x} ${cp1y}, ${cp2x} ${cp2y}, ${p2[0]} ${p2[1]}`;
    }
  } else {
    for (let i = 1; i < pts.length; i++) {
      d += ` L ${pts[i][0]} ${pts[i][1]}`;
    }
  }
  // SVG <linearGradient> ids must be unique per render to avoid the
  // browser sharing one gradient definition between sparklines and
  // tinting them all the same colour. `useId()` (computed above) gives a
  // stable, collision-free id that does not churn on every render.
  const id = `spark-grad-${reactId.replace(/:/g, "")}`;
  return (
    <svg
      width={width}
      height={height}
      style={{ display: "block" }}
      role={ariaLabel ? "img" : undefined}
      aria-label={ariaLabel}
    >
      <defs>
        <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity={0.35} />
          <stop offset="100%" stopColor={color} stopOpacity={0} />
        </linearGradient>
      </defs>
      {fill && (
        <path d={`${d} L ${width} ${height} L 0 ${height} Z`} fill={`url(#${id})`} />
      )}
      <path
        d={d}
        fill="none"
        stroke={color}
        strokeWidth={strokeWidth}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  );
}

/* -------------------------------------------------------------------------- */
/* Eyebrow / NumberDisplay / PressureBar                                      */
/* -------------------------------------------------------------------------- */

export function Eyebrow({ children }: { children: ReactNode }) {
  return (
    <div
      style={{
        fontSize: 10,
        fontWeight: 600,
        color: "var(--text-faint)",
        textTransform: "uppercase",
        letterSpacing: "0.12em",
      }}
    >
      {children}
    </div>
  );
}

export function NumberDisplay({
  value,
  unit,
  size = "lg",
  tone,
}: {
  value: string;
  unit?: string;
  size?: "lg" | "xl" | "hero";
  tone?: string;
}) {
  const fontSize = size === "hero" ? 40 : size === "xl" ? 28 : 20;
  const weight = size === "hero" ? 600 : 700;
  return (
    <div
      style={{
        fontSize,
        fontWeight: weight,
        color: tone ?? "var(--text-primary)",
        letterSpacing: "-0.025em",
        fontVariantNumeric: "tabular-nums",
        lineHeight: 1.05,
        display: "inline-flex",
        alignItems: "baseline",
        gap: 4,
      }}
    >
      {value}
      {unit && (
        <span
          style={{
            fontSize: Math.round(fontSize * 0.4),
            color: "var(--text-faint)",
            fontWeight: 500,
          }}
        >
          {unit}
        </span>
      )}
    </div>
  );
}

export function PressureBar({ pct, color }: { pct: number; color: string }) {
  const safe = Number.isFinite(pct) ? Math.max(0, Math.min(1, pct)) : 0;
  return (
    <div
      style={{
        height: 4,
        background: "var(--kpi-bar-bg)",
        borderRadius: 999,
        overflow: "hidden",
      }}
    >
      <div
        style={{
          width: `${safe * 100}%`,
          height: "100%",
          background: color,
          transition: "width 200ms ease-out",
        }}
      />
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* KpiInline — used inside the Pulse strip                                    */
/* -------------------------------------------------------------------------- */

export function KpiInline({
  icon,
  label,
  value,
  tone,
  bar,
  hint,
  title,
}: {
  icon: ReactNode;
  label: string;
  value: string;
  tone: string;
  /** Optional 0..1 pressure bar shown beneath the value. */
  bar?: number;
  hint?: string;
  /** Native tooltip surfaced on hover (e.g. peak node name). */
  title?: string;
}) {
  return (
    <div
      style={{ display: "flex", flexDirection: "column", gap: 6, flex: 1, minWidth: 0 }}
      title={title}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          fontSize: 10,
          color: "var(--text-muted)",
          textTransform: "uppercase",
          letterSpacing: "0.08em",
          fontWeight: 600,
        }}
      >
        {icon}
        <span>{label}</span>
      </div>
      <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
        <span
          style={{
            fontSize: 18,
            fontWeight: 700,
            color: tone,
            fontVariantNumeric: "tabular-nums",
            letterSpacing: "-0.01em",
            lineHeight: 1,
          }}
        >
          {value}
        </span>
        {hint && (
          <span
            style={{
              fontSize: 10,
              color: "var(--text-faint)",
              fontVariantNumeric: "tabular-nums",
            }}
          >
            {hint}
          </span>
        )}
      </div>
      {bar !== undefined && <PressureBar pct={bar} color={tone} />}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Job state badge + split progress                                           */
/* -------------------------------------------------------------------------- */

const JOB_STATE_TONES: Record<DisplayJobState, { color: string; bg: string }> = {
  Queued: { color: "var(--text-muted)", bg: "rgba(157,165,180,0.10)" },
  Pending: { color: "var(--text-faint)", bg: "rgba(255,255,255,0.04)" },
  Running: { color: "var(--accent)", bg: "rgba(110,159,255,0.10)" },
  Reducing: { color: "var(--purple)", bg: "rgba(180,130,255,0.10)" },
  Completed: { color: "var(--success)", bg: "rgba(115,191,105,0.10)" },
  Failed: { color: "var(--danger)", bg: "rgba(242,114,111,0.10)" },
  Unknown: { color: "var(--text-faint)", bg: "rgba(255,255,255,0.04)" },
};

export function JobStateBadge({ s }: { s: DisplayJobState }) {
  const m = JOB_STATE_TONES[s];
  return (
    <span
      style={{
        fontSize: 10,
        fontWeight: 600,
        letterSpacing: "0.04em",
        padding: "2px 7px",
        borderRadius: 4,
        background: m.bg,
        color: m.color,
        border: `1px solid ${m.color}33`,
      }}
    >
      {s.toUpperCase()}
    </span>
  );
}

export function SplitProgress({
  done,
  total,
  color = "var(--accent)",
}: {
  done: number;
  total: number;
  color?: string;
}) {
  const pct = total === 0 ? 0 : Math.max(0, Math.min(1, done / total));
  return (
    <div
      style={{ display: "flex", gap: 2, width: 76, height: 4, alignItems: "center" }}
      title={total === 0 ? "splits unknown" : `${done} / ${total} splits`}
    >
      {Array.from({ length: 10 }).map((_, i) => {
        const filled = i < Math.round(pct * 10);
        return (
          <div
            key={i}
            style={{
              flex: 1,
              height: "100%",
              borderRadius: 1,
              background: filled ? color : "rgba(255,255,255,0.07)",
            }}
          />
        );
      })}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* JobRow — one BLAST job row in the active jobs cell                         */
/* -------------------------------------------------------------------------- */

export function fmtDuration(sec: number | null | undefined): string {
  if (sec == null || !Number.isFinite(sec) || sec < 0) return "—";
  if (sec < 60) return `${Math.round(sec)}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${Math.round(sec % 60)}s`;
  return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`;
}

function rowBackground(state: DisplayJobState): string {
  switch (state) {
    case "Failed":
      return "rgba(242,114,111,0.07)";
    case "Reducing":
      return "rgba(184,119,217,0.08)";
    case "Running":
      return "rgba(110,159,255,0.07)";
    case "Completed":
      return "rgba(115,191,105,0.06)";
    default:
      return "rgba(255,255,255,0.025)";
  }
}

/**
 * Single-line job row for the AKS card "Active jobs" cell.
 *
 * Layout (pipe-separated):
 *   {program} | {db} | {title} | {STATE} | {age}(age) | {duration}(duration)
 *
 * - State is rendered as bold uppercase text in the state tone (no pill —
 *   the row's left-edge colored bar plus the colored state text are the
 *   visual signal).
 * - `age` is wall-clock time since `createdAt` (keeps ticking even for
 *   terminal jobs). `duration` is the actual compute time
 *   (`elapsedSec`), which freezes when the job reaches a terminal state.
 * - Title overflow is ellipsized; the full title is in the row's
 *   `title` tooltip.
 */
export function JobRow({ j, dense = false }: { j: JobRowView; dense?: boolean }) {
  const tone = JOB_STATE_TONES[j.state].color;
  const bg = rowBackground(j.state);
  const createdMs = j.createdAt ? Date.parse(j.createdAt) : NaN;
  const ageSec = Number.isFinite(createdMs)
    ? Math.max(0, (Date.now() - createdMs) / 1000)
    : null;
  const durationSec =
    j.elapsedSec != null && Number.isFinite(j.elapsedSec) ? j.elapsedSec : ageSec;
  const displayTitle = j.title || j.jobId;
  const fullTitle = `${j.program} | ${j.db} | ${displayTitle} | ${j.state.toUpperCase()}`;
  return (
    <div
      title={fullTitle}
      style={{
        display: "flex",
        alignItems: "center",
        minWidth: 0,
        padding: dense ? "5px 10px" : "7px 12px",
        borderRadius: 7,
        background: bg,
        border: "1px solid rgba(255,255,255,0.04)",
        borderLeft: `3px solid ${tone}`,
        fontSize: 11.5,
        fontFamily: "var(--font-mono)",
        fontVariantNumeric: "tabular-nums",
        whiteSpace: "nowrap",
        gap: 0,
      }}
    >
      <JobSegment>{j.program}</JobSegment>
      <JobPipe />
      <JobSegment>{j.db}</JobSegment>
      <JobPipe />
      <span
        style={{
          flex: 1,
          minWidth: 0,
          overflow: "hidden",
          textOverflow: "ellipsis",
          color: "var(--text-primary)",
        }}
      >
        {displayTitle}
      </span>
      <JobPipe />
      <span style={{ color: tone, fontWeight: 600, letterSpacing: "0.04em" }}>
        {j.state.toUpperCase()}
      </span>
      <JobPipe />
      <span>
        <strong style={{ color: "var(--text-primary)", fontWeight: 600 }}>
          {fmtDuration(ageSec)}
        </strong>
        <span style={{ color: "var(--text-faint)", marginLeft: 2 }}>(age)</span>
      </span>
      <JobPipe />
      <span>
        <strong style={{ color: "var(--text-primary)", fontWeight: 600 }}>
          {fmtDuration(durationSec)}
        </strong>
        <span style={{ color: "var(--text-faint)", marginLeft: 2 }}>(duration)</span>
      </span>
    </div>
  );
}

function JobSegment({ children }: { children: ReactNode }) {
  return (
    <span
      style={{
        color: "var(--text-muted)",
        flexShrink: 0,
        maxWidth: 200,
        overflow: "hidden",
        textOverflow: "ellipsis",
      }}
    >
      {children}
    </span>
  );
}

function JobPipe() {
  return (
    <span
      aria-hidden="true"
      style={{ color: "var(--text-faint)", margin: "0 8px", flexShrink: 0 }}
    >
      |
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/* EventLine — one row in the Live Activity rail                              */
/* -------------------------------------------------------------------------- */

export type EventKind = "ok" | "info" | "warn" | "err";

export function EventLine({
  kind,
  message,
  time,
  compact = false,
}: {
  kind: EventKind;
  message: string;
  time: string;
  compact?: boolean;
}) {
  const color =
    kind === "err"
      ? "var(--danger)"
      : kind === "warn"
        ? "var(--warning)"
        : kind === "info"
          ? "var(--accent)"
          : "var(--success)";
  const Icon =
    kind === "err"
      ? XCircle
      : kind === "warn"
        ? AlertTriangle
        : kind === "info"
          ? Info
          : CheckCircle2;
  const looksLikeMono = /^(POST|GET|PUT|DELETE|blast-|pod\/|svc\/|node\/|\[)/.test(message);
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: compact ? "5px 10px" : "7px 12px",
        background: "transparent",
        borderRadius: 6,
        fontSize: 11.5,
        lineHeight: 1.4,
      }}
    >
      <Icon size={11} color={color} style={{ flexShrink: 0 }} />
      <span
        style={{
          flex: 1,
          color: "var(--text-primary)",
          fontFamily: looksLikeMono ? "var(--font-mono)" : undefined,
          fontSize: 11,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
        title={message}
      >
        {message}
      </span>
      <span
        style={{
          fontSize: 10,
          color: "var(--text-faint)",
          flexShrink: 0,
          fontVariantNumeric: "tabular-nums",
        }}
      >
        {time}
      </span>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* BentoCell — uniform glass cell wrapper                                     */
/* -------------------------------------------------------------------------- */

export function BentoCell({
  children,
  span,
  accent,
  style,
}: {
  children: ReactNode;
  span?: [number, number];
  accent?: string;
  style?: CSSProperties;
}) {
  return (
    <div
      className="bento-cell"
      style={{
        gridColumn: span ? `span ${span[0]}` : undefined,
        gridRow: span ? `span ${span[1]}` : undefined,
        padding: "14px 16px",
        display: "flex",
        flexDirection: "column",
        minHeight: 0,
        position: "relative",
        overflow: "hidden",
        ...style,
      }}
    >
      {accent && (
        <div
          className="bento-cell__accent"
          style={{
            background: `linear-gradient(90deg, transparent 0%, ${accent} 30%, ${accent} 70%, transparent 100%)`,
          }}
        />
      )}
      {children}
    </div>
  );
}
