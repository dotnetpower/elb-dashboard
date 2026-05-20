/**
 * PulseRowSummary — the always-visible header button: health dot +
 * cluster name + sub-status line + three live stats + chevron.
 *
 * Receives only display props; data shaping lives in the parent.
 */

import { Activity, ChevronDown, ChevronRight, Flame, Send } from "lucide-react";

import { HealthDot, PulseStat } from "./atoms";
import type { HealthTone } from "./helpers";

interface Props {
  clusterName: string;
  tone: HealthTone;
  statusTone: HealthTone;
  statusLine: string;
  submits15m: string;
  activeCount: string;
  activeTone?: string;
  pressureLabel: string;
  pressureTone?: string;
  open: boolean;
  onToggle: () => void;
  /** id of the panel this button controls (for `aria-controls`). */
  panelId?: string;
}

export function PulseRowSummary({
  clusterName,
  tone,
  statusTone,
  statusLine,
  submits15m,
  activeCount,
  activeTone,
  pressureLabel,
  pressureTone,
  open,
  onToggle,
  panelId,
}: Props) {
  const statusColor =
    statusTone === "healthy"
      ? "var(--success)"
      : statusTone === "degraded"
        ? "var(--warning)"
        : statusTone === "down"
          ? "var(--danger)"
          : statusTone === "transitioning"
            ? "var(--accent)"
            : "var(--text-faint)";

  return (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={open}
      aria-controls={panelId}
      aria-label={`${clusterName} \u2014 ${statusLine}. ${open ? "Collapse" : "Expand"} cluster row.`}
      title={open ? "Collapse cluster" : "Expand cluster"}
      style={{
        width: "100%",
        background: "transparent",
        border: "none",
        padding: "8px 10px",
        display: "grid",
        gridTemplateColumns: "auto minmax(0, 1fr) auto auto auto 14px",
        alignItems: "center",
        gap: 10,
        cursor: "pointer",
        color: "inherit",
        textAlign: "left",
      }}
    >
      <HealthDot tone={tone} />
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          gap: 2,
          minWidth: 0,
        }}
      >
        <span
          title={clusterName}
          style={{
            fontSize: 13,
            fontWeight: 600,
            lineHeight: 1.15,
            color: "var(--text-primary)",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {clusterName}
        </span>
        <span
          title={statusLine}
          style={{
            fontSize: 11,
            lineHeight: 1.2,
            color: statusColor,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {statusLine}
        </span>
      </div>
      <PulseStat
        label="Submits 15m"
        value={submits15m}
        icon={<Send size={11} aria-hidden="true" />}
        tooltip="Jobs created in the last 15 minutes"
      />
      <PulseStat
        label="Active"
        value={activeCount}
        icon={<Activity size={11} aria-hidden="true" />}
        tone={activeTone}
        tooltip="Jobs currently Pending, Running or Reducing"
      />
      <PulseStat
        label="Pressure"
        value={pressureLabel}
        icon={<Flame size={11} aria-hidden="true" />}
        tone={pressureTone}
        tooltip="Higher of CPU peak and Mem peak across user-pool nodes"
      />
      {open ? (
        <ChevronDown size={14} color="var(--text-faint)" aria-hidden="true" />
      ) : (
        <ChevronRight size={14} color="var(--text-faint)" aria-hidden="true" />
      )}
    </button>
  );
}
