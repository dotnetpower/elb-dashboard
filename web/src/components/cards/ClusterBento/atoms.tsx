/**
 * Reusable visual atoms for the cluster bento layout.
 *
 * These are the production counterparts of the prototype atoms in
 * `web/src/pages/mockups/AksCardMockupsPremium.tsx`. They are kept
 * deliberately self-contained — no contextual state, no fetches —
 * so they remain easy to reuse from other dashboard surfaces (per-job
 * details, modal, BLAST analytics).
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
import type { CSSProperties, ReactNode } from "react";

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
  // tinting them all the same colour. Math.random in render is fine
  // here because the value is purely visual; React's reconciliation
  // doesn't care.
  const id = `spark-grad-${Math.random().toString(36).slice(2, 10)}`;
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

export type DisplayJobState =
  | "Pending"
  | "Running"
  | "Reducing"
  | "Completed"
  | "Failed"
  | "Unknown";

const JOB_STATE_TONES: Record<DisplayJobState, { color: string; bg: string }> = {
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

export interface JobRowView {
  jobId: string;
  /** Truncated/display id (e.g. last 8 chars). Falls back to `jobId`. */
  displayId?: string;
  title: string;
  db: string;
  query: string;
  state: DisplayJobState;
  /** Absolute creation time (ISO-8601). Used to compute elapsed seconds. */
  createdAt?: string | null;
  /** Optional pre-computed elapsed seconds (overrides createdAt-based math). */
  elapsedSec?: number | null;
  /** Optional ETA in seconds. */
  etaSec?: number | null;
  splitsDone?: number | null;
  splitsTotal?: number | null;
  note?: string | null;
}

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

export function JobRow({ j, dense = false }: { j: JobRowView; dense?: boolean }) {
  const tone = JOB_STATE_TONES[j.state].color;
  const bg = rowBackground(j.state);
  const elapsed =
    j.elapsedSec != null
      ? j.elapsedSec
      : j.createdAt
        ? Math.max(0, (Date.now() - new Date(j.createdAt).getTime()) / 1000)
        : null;
  const splitsDone = j.splitsDone ?? 0;
  const splitsTotal = j.splitsTotal ?? 0;
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "82px 1fr 90px 96px auto",
        alignItems: "center",
        gap: 12,
        padding: dense ? "6px 10px" : "8px 12px",
        borderRadius: 7,
        background: bg,
        border: "1px solid rgba(255,255,255,0.04)",
        fontSize: 11.5,
      }}
    >
      <span
        style={{
          fontFamily: "var(--font-mono)",
          fontSize: 11,
          color: "var(--text-primary)",
          fontWeight: 500,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
        title={j.jobId}
      >
        {j.displayId ?? j.jobId.slice(0, 8)}
      </span>
      <span
        style={{
          color: "var(--text-muted)",
          fontSize: 11,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
      >
        <span style={{ color: "var(--text-primary)", fontWeight: 500 }}>{j.title || j.db || "—"}</span>
        {j.note ? (
          <span style={{ color: tone, marginLeft: 6 }}>· {j.note}</span>
        ) : j.query ? (
          <span style={{ marginLeft: 6 }}>· {j.query}</span>
        ) : null}
      </span>
      <SplitProgress done={splitsDone} total={splitsTotal} color={tone} />
      <span
        style={{
          fontVariantNumeric: "tabular-nums",
          fontSize: 11,
          color: "var(--text-muted)",
          textAlign: "right",
        }}
      >
        {j.state === "Pending"
          ? "queued"
          : j.etaSec
            ? `${fmtDuration(elapsed)} · ETA ${fmtDuration(j.etaSec)}`
            : fmtDuration(elapsed)}
      </span>
      <JobStateBadge s={j.state} />
    </div>
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
      style={{
        gridColumn: span ? `span ${span[0]}` : undefined,
        gridRow: span ? `span ${span[1]}` : undefined,
        padding: "14px 16px",
        background:
          "linear-gradient(160deg, rgba(255,255,255,0.025) 0%, rgba(255,255,255,0.005) 100%)",
        border: "1px solid var(--border-weak)",
        borderRadius: 12,
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
          style={{
            position: "absolute",
            top: 0,
            left: 0,
            right: 0,
            height: 1,
            background: `linear-gradient(90deg, transparent 0%, ${accent} 30%, ${accent} 70%, transparent 100%)`,
            opacity: 0.5,
          }}
        />
      )}
      {children}
    </div>
  );
}
