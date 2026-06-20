import { ChevronDown, Flame, Loader2, Play, Square, Trash2 } from "lucide-react";

import type { AksClusterSummary } from "@/api/endpoints";
import {
  getAksProvisioningLabel,
  isAksProvisioned,
  isAksProvisioning,
} from "@/utils/aksStatus";

export function ClusterHeaderBand({
  cluster: c,
  collapsed,
  onToggleCollapse,
  trans,
  isRunning,
  isWarm,
  warmupDbsCount,
  actionLoading,
  onStartStop,
  onDelete,
}: {
  cluster: AksClusterSummary;
  collapsed: boolean;
  onToggleCollapse: () => void;
  trans: "starting" | "stopping" | undefined;
  isRunning: boolean;
  isWarm: boolean;
  warmupDbsCount: number;
  actionLoading: string | null;
  onStartStop: (name: string, action: "start" | "stop") => void;
  onDelete: (name: string) => void;
}) {
  const provisioningLabel = getAksProvisioningLabel(c);
  const canControlPower = isAksProvisioned(c);
  const busyProvisioning = isAksProvisioning(c);
  const powerLabel =
    trans === "starting"
      ? "Starting..."
      : trans === "stopping"
        ? "Stopping..."
        : (provisioningLabel ?? c.power_state ?? "?");
  const powerColor =
    trans === "starting"
      ? "var(--accent)"
      : trans === "stopping"
        ? "var(--warning)"
        : provisioningLabel === "Failed"
          ? "var(--danger)"
          : busyProvisioning
            ? "var(--accent)"
            : c.power_state === "Running"
              ? "var(--success)"
              : "var(--warning)";

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        padding: "10px 14px",
        cursor: "pointer",
        flexWrap: "wrap",
        borderBottom: collapsed ? "none" : "1px solid var(--border-weak)",
        background: "var(--bg-tertiary)",
      }}
      onClick={onToggleCollapse}
    >
      <ChevronDown
        size={14}
        style={{
          transform: collapsed ? "rotate(-90deg)" : "rotate(0)",
          transition: "transform 0.15s",
          color: "var(--text-faint)",
          flexShrink: 0,
        }}
        aria-hidden="true"
      />
      <strong
        style={{
          fontSize: 14,
          letterSpacing: "0.01em",
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
        }}
        title={collapsed ? "Click row to expand cluster details" : "Click row to collapse cluster details"}
      >
        {c.name}
      </strong>
      <span
        style={{ fontSize: 11, color: powerColor, fontWeight: 600, flexShrink: 0 }}
      >
        {(trans === "starting" || trans === "stopping" || busyProvisioning) && (
          <Loader2
            size={10}
            className="spin"
            style={{ verticalAlign: "middle", marginRight: 3 }}
          />
        )}
        {powerLabel}
      </span>
      {/* #7 — Workspace ready chip lives next to the power label so the
          "is this cluster usable?" signal stays in one place. */}
      {isRunning && isWarm && (
        <span
          className="dv3-warmup-chip"
          style={{ fontSize: 10, padding: "2px 7px" }}
          title={
            warmupDbsCount > 0
              ? `${warmupDbsCount} database${warmupDbsCount === 1 ? "" : "s"} warmed`
              : "Workspace ready"
          }
        >
          <Flame size={10} strokeWidth={1.75} /> ready
        </span>
      )}
      <span
        className="muted"
        style={{
          fontSize: 11,
          display: "inline-flex",
          gap: 8,
          flexWrap: "wrap",
          flexShrink: 1,
          minWidth: 0,
        }}
      >
        <span>· {c.region}</span>
        <span>· k8s {c.k8s_version ?? "?"}</span>
        {(c.agent_pools?.length ?? 0) === 0 && (
          <>
            <span>· {c.node_count ?? "?"} nodes</span>
            <span>({c.node_sku ?? "?"})</span>
          </>
        )}
      </span>
      {/* #11 — Stop/Delete grouped behind a vertical divider, pushed to the
          far right so destructive actions don't sit shoulder-to-shoulder
          with the cluster name. */}
      <div
        style={{
          display: "flex",
          gap: "var(--space-2)",
          alignItems: "center",
          flexShrink: 0,
          marginLeft: "auto",
          paddingLeft: 10,
          borderLeft: "1px solid var(--border-weak)",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {!trans && canControlPower && c.power_state === "Stopped" && (
          <button
            className="glass-button"
            onClick={() => onStartStop(c.name, "start")}
            disabled={actionLoading !== null}
            style={{ fontSize: 10, padding: "2px 8px", color: "var(--success)" }}
            title="Start cluster"
          >
            {actionLoading === `start-${c.name}` ? (
              <Loader2 size={10} className="spin" />
            ) : (
              <Play size={10} strokeWidth={1.5} />
            )}{" "}
            Start
          </button>
        )}
        {!trans && canControlPower && c.power_state === "Running" && (
          <button
            className="glass-button"
            onClick={() => onStartStop(c.name, "stop")}
            disabled={actionLoading !== null}
            style={{ fontSize: 10, padding: "2px 8px", color: "var(--warning)" }}
            title="Stop cluster (saves cost)"
          >
            {actionLoading === `stop-${c.name}` ? (
              <Loader2 size={10} className="spin" />
            ) : (
              <Square size={10} strokeWidth={1.5} />
            )}{" "}
            Stop
          </button>
        )}
        <button
          className="glass-button"
          onClick={() => onDelete(c.name)}
          disabled={actionLoading !== null}
          style={{ fontSize: 10, padding: "2px 8px", color: "var(--danger)" }}
          title="Delete cluster"
        >
          {actionLoading === `delete-${c.name}` ? (
            <Loader2 size={10} className="spin" />
          ) : (
            <Trash2 size={10} strokeWidth={1.5} />
          )}
        </button>
      </div>
    </div>
  );
}
