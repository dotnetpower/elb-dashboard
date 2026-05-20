/**
 * ClusterPulse — small, reusable UI atoms with no data dependencies.
 *
 * Every component here takes already-resolved props (numbers, strings,
 * tone colours) so they can be unit-tested without mounting the whole
 * cluster card. Anything that needs queries or state belongs in a
 * higher-level module (see `usePulseSignals`, `JobsSection`, etc.).
 */

import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  Database,
  Loader2,
  XCircle,
} from "lucide-react";

import type { DisplayJobState } from "@/components/cards/ClusterBento/jobTypes";

import { jobStateTone, toneColor, type HealthTone } from "./helpers";

export function HealthDot({
  tone,
  size = 8,
}: {
  tone: HealthTone;
  size?: number;
}) {
  const color = toneColor(tone);
  if (tone === "transitioning") {
    return (
      <Loader2
        size={size + 2}
        className="spin"
        color={color}
        strokeWidth={2.5}
        style={{ flexShrink: 0 }}
        aria-label="Cluster transitioning"
      />
    );
  }
  return (
    <span
      role="img"
      aria-label={`Cluster ${tone}`}
      style={{
        display: "inline-block",
        width: size,
        height: size,
        borderRadius: "50%",
        background: color,
        boxShadow:
          tone === "healthy" ? `0 0 6px ${color}88` : `0 0 8px ${color}cc`,
        flexShrink: 0,
      }}
    />
  );
}

export function PulseStat({
  label,
  value,
  icon,
  tone,
  tooltip,
}: {
  label: string;
  value: string;
  icon: React.ReactNode;
  tone?: string;
  /** Hover help text shown on the whole stat block. */
  tooltip?: string;
}) {
  return (
    <div
      title={tooltip}
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "flex-end",
        gap: 1,
        minWidth: 88,
        cursor: tooltip ? "help" : undefined,
      }}
    >
      <span
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 4,
          fontSize: 10,
          color: "var(--text-faint)",
          textTransform: "uppercase",
          letterSpacing: "0.08em",
        }}
      >
        {icon}
        {label}
      </span>
      <span
        style={{
          fontSize: 16,
          fontWeight: 600,
          color: tone ?? "var(--text-primary)",
          fontVariantNumeric: "tabular-nums",
          lineHeight: 1.1,
        }}
      >
        {value}
      </span>
    </div>
  );
}

export function MetaCell({
  label,
  value,
  tone,
  tooltip,
}: {
  label: string;
  value: string;
  tone?: string;
  /** Hover help text shown on the whole cell. */
  tooltip?: string;
}) {
  return (
    <div
      title={tooltip}
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 2,
        cursor: tooltip ? "help" : undefined,
      }}
    >
      <span
        style={{
          fontSize: 10,
          color: "var(--text-faint)",
          textTransform: "uppercase",
          letterSpacing: "0.08em",
        }}
      >
        {label}
      </span>
      <span
        style={{
          fontSize: 12,
          fontWeight: 500,
          color: tone ?? "var(--text-primary)",
          fontVariantNumeric: "tabular-nums",
        }}
      >
        {value}
      </span>
    </div>
  );
}

export function ActionBtn({
  tone,
  children,
  icon,
  onClick,
  disabled,
}: {
  tone: "success" | "warning" | "danger" | "neutral" | "accent";
  children: React.ReactNode;
  icon?: React.ReactNode;
  onClick?: () => void;
  disabled?: boolean;
}) {
  const color =
    tone === "success"
      ? "var(--success)"
      : tone === "warning"
        ? "var(--warning)"
        : tone === "danger"
          ? "var(--danger)"
          : tone === "accent"
            ? "var(--accent)"
            : "var(--text-muted)";
  const borderColor = tone === "neutral" ? "var(--border-medium)" : color;
  const background = tone === "accent" ? "rgba(122, 167, 255, 0.12)" : "transparent";
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        padding: "5px 11px",
        fontSize: 11,
        fontWeight: 500,
        color,
        background,
        border: `1px solid ${borderColor}`,
        borderRadius: 7,
        cursor: disabled ? "not-allowed" : "pointer",
        opacity: disabled ? 0.55 : 1,
      }}
    >
      {icon}
      {children}
    </button>
  );
}

export function DbChip({ name }: { name: string }) {
  return (
    <span
      title={name}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        padding: "2px 8px",
        borderRadius: 4,
        background: "var(--bg-canvas)",
        border: "1px solid var(--border-weak)",
        color: "var(--text-muted)",
        fontSize: 10,
        fontFamily: "var(--font-mono)",
        overflow: "hidden",
        textOverflow: "ellipsis",
        whiteSpace: "nowrap",
        justifyContent: "center",
      }}
    >
      <Database size={9} aria-hidden="true" />
      {name}
    </span>
  );
}

export function JobStatePill({ state }: { state: DisplayJobState }) {
  const tone = jobStateTone(state);
  const Icon =
    state === "Running" || state === "Reducing"
      ? Loader2
      : state === "Completed"
        ? CheckCircle2
        : state === "Failed"
          ? XCircle
          : state === "Unknown"
            ? AlertTriangle
            : Clock; // Pending
  const spinning = state === "Running" || state === "Reducing";
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        padding: "2px 7px",
        borderRadius: 4,
        background: `${tone}1a`,
        border: `1px solid ${tone}44`,
        color: tone,
        fontSize: 10,
        fontWeight: 600,
        letterSpacing: "0.02em",
        textTransform: "uppercase",
        justifyContent: "center",
      }}
    >
      <Icon
        size={10}
        strokeWidth={2.2}
        className={spinning ? "spin" : undefined}
        aria-hidden="true"
      />
      {state}
    </span>
  );
}
