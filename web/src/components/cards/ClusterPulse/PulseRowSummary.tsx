/**
 * PulseRowSummary — the always-visible header button: health dot +
 * cluster name + sub-status line + three live stats + chevron.
 *
 * Receives only display props; data shaping lives in the parent.
 */

import { Activity, ChevronDown, ChevronRight, Flame, Send } from "lucide-react";

import { HealthDot, PulseStat } from "./atoms";
import { tierTone, type HealthTone } from "./helpers";

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
  /** Optional `elb-tier` ARM tag (heavy / light / gpu / general). */
  tier?: string | null;
  /** Optional cluster resource group — shown next to the name when the
   * card is operating sub-wide and clusters may live in different RGs. */
  resourceGroup?: string;
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
  tier,
  resourceGroup,
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
        gridTemplateColumns: "auto minmax(0, 1fr) auto 14px",
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
            display: "flex",
            alignItems: "center",
            gap: 6,
            minWidth: 0,
          }}
        >
          <span
            style={{
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
              minWidth: 0,
            }}
          >
            {clusterName}
          </span>
          {tier ? (
            <span
              title={`elb-tier: ${tier}`}
              style={{
                flexShrink: 0,
                fontSize: 9,
                fontWeight: 600,
                lineHeight: 1,
                padding: "2px 6px",
                borderRadius: 999,
                background: tierTone(tier).background,
                color: tierTone(tier).color,
                border: tierTone(tier).border,
                textTransform: "lowercase",
                letterSpacing: 0.2,
              }}
            >
              {tier}
            </span>
          ) : null}
          {resourceGroup ? (
            <span
              title={`Resource group: ${resourceGroup}`}
              style={{
                flexShrink: 0,
                fontSize: 9,
                fontWeight: 500,
                lineHeight: 1,
                color: "var(--text-faint)",
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
                maxWidth: 160,
              }}
            >
              {resourceGroup}
            </span>
          ) : null}
        </span>
        <span
          title={statusLine}
          className="pulse-row-subline"
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
      <div className="pulse-row-stats" style={{ display: "flex", alignItems: "center", gap: 10 }}>
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
          label="Load"
          value={pressureLabel}
          icon={<Flame size={11} aria-hidden="true" />}
          tone={pressureTone}
          tooltip="max(CPU peak, Mem peak) across user-pool nodes"
        />
      </div>
      {open ? (
        <ChevronDown size={14} color="var(--text-faint)" aria-hidden="true" />
      ) : (
        <ChevronRight size={14} color="var(--text-faint)" aria-hidden="true" />
      )}
    </button>
  );
}
