/**
 * PulseActions — Start / Stop / Open detail / Delete buttons rendered
 * inside the expanded panel.
 */

import { Loader2, Play, Square, Trash2 } from "lucide-react";

import type { AksClusterSummary } from "@/api/endpoints";
import { isAksProvisioned } from "@/utils/aksStatus";

import { ActionBtn } from "./atoms";

interface Props {
  cluster: AksClusterSummary;
  trans?: "starting" | "stopping";
  actionLoading: string | null;
  onStartStop: (name: string, action: "start" | "stop") => void;
  onDelete: (name: string) => void;
  onOpenDetail: () => void;
}

export function PulseActions({
  cluster: c,
  trans,
  actionLoading,
  onStartStop,
  onDelete,
  onOpenDetail,
}: Props) {
  const canControlPower = isAksProvisioned(c);
  const busy = actionLoading !== null;
  return (
    <div
      style={{
        padding: "10px 14px",
        borderBottom: "1px solid var(--border-weak)",
        display: "flex",
        gap: 8,
        flexWrap: "wrap",
      }}
    >
      {!trans && canControlPower && c.power_state === "Stopped" && (
        <span title="Start the AKS cluster (~5 min to ready)">
          <ActionBtn
            tone="success"
            disabled={busy}
            onClick={() => onStartStop(c.name, "start")}
            icon={
              actionLoading === `start-${c.name}` ? (
                <Loader2 size={11} className="spin" aria-hidden="true" />
              ) : (
                <Play size={11} aria-hidden="true" />
              )
            }
          >
            Start
          </ActionBtn>
        </span>
      )}
      {!trans && canControlPower && c.power_state === "Running" && (
        <span title="Stop the AKS cluster — paused billing, no running jobs">
          <ActionBtn
            tone="warning"
            disabled={busy}
            onClick={() => onStartStop(c.name, "stop")}
            icon={
              actionLoading === `stop-${c.name}` ? (
                <Loader2 size={11} className="spin" aria-hidden="true" />
              ) : (
                <Square size={11} aria-hidden="true" />
              )
            }
          >
            Stop
          </ActionBtn>
        </span>
      )}
      <span title="Open the per-cluster detail modal (node pools, identity, network)">
        <ActionBtn tone="neutral" onClick={onOpenDetail}>
          Open cluster detail
        </ActionBtn>
      </span>
      <span title="Delete the cluster and its node pools (irreversible)">
        <ActionBtn
          tone="danger"
          disabled={busy}
          onClick={() => onDelete(c.name)}
          icon={
            actionLoading === `delete-${c.name}` ? (
              <Loader2 size={11} className="spin" aria-hidden="true" />
            ) : (
              <Trash2 size={11} aria-hidden="true" />
            )
          }
        >
          Delete
        </ActionBtn>
      </span>
    </div>
  );
}
