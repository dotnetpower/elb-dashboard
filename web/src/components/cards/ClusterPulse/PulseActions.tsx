/**
 * PulseActions — Start / Stop / Open detail / Delete buttons rendered
 * inside the expanded panel.
 */

import { Info, Loader2, Play, Square, Trash2 } from "lucide-react";

import type { AksClusterSummary } from "@/api/endpoints";
import type { ClusterTransitionKind } from "@/components/cards/ClusterCard/useClusterActions";
import { isAksProvisioned } from "@/utils/aksStatus";

import { ActionBtn } from "./atoms";

interface Props {
  cluster: AksClusterSummary;
  trans?: ClusterTransitionKind;
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
        padding: "6px 10px",
        borderBottom: "1px solid var(--border-weak)",
        display: "flex",
        justifyContent: "space-between",
        alignItems: "center",
        gap: 6,
        flexWrap: "wrap",
      }}
    >
      <span title="Open the per-cluster detail modal (node pools, identity, network)">
        <ActionBtn
          tone="accent"
          onClick={onOpenDetail}
          icon={<Info size={11} aria-hidden="true" />}
        >
          Open cluster detail
        </ActionBtn>
      </span>
      <div
        className="dashboard-hide-mobile"
        style={{
          display: "flex",
          gap: 6,
          marginLeft: "auto",
          flexWrap: "wrap",
          justifyContent: "flex-end",
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
          <span title="Stop the AKS cluster - paused billing, no running jobs">
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
    </div>
  );
}
