/**
 * PulseActions — Start / Stop / Open detail / Open in Portal / Delete
 * buttons rendered inside the expanded panel.
 */

import { ExternalLink, Info, Loader2, Play, Square, Trash2 } from "lucide-react";

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
  /** Azure subscription id — used to build the cluster's Portal URL. */
  subscriptionId: string;
}

function clusterPortalUrl(args: {
  subscriptionId: string;
  resourceGroup: string;
  clusterName: string;
}): string {
  const { subscriptionId, resourceGroup, clusterName } = args;
  const path = `/subscriptions/${encodeURIComponent(
    subscriptionId,
  )}/resourceGroups/${encodeURIComponent(
    resourceGroup,
  )}/providers/Microsoft.ContainerService/managedClusters/${encodeURIComponent(
    clusterName,
  )}/overview`;
  return `https://portal.azure.com/#@/resource${path}`;
}

export function PulseActions({
  cluster: c,
  trans,
  actionLoading,
  onStartStop,
  onDelete,
  onOpenDetail,
  subscriptionId,
}: Props) {
  const canControlPower = isAksProvisioned(c);
  const busy = actionLoading !== null;
  const portalHref = subscriptionId
    ? clusterPortalUrl({
        subscriptionId,
        resourceGroup: c.resource_group,
        clusterName: c.name,
      })
    : null;
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
      <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
        <span title="Open the per-cluster detail modal (node pools, identity, network)">
          <ActionBtn
            tone="accent"
            onClick={onOpenDetail}
            icon={<Info size={11} aria-hidden="true" />}
          >
            Open cluster detail
          </ActionBtn>
        </span>
        {portalHref && (
          <a
            href={portalHref}
            target="_blank"
            rel="noopener noreferrer"
            title="Open this AKS cluster in the Azure Portal"
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
              padding: "3px 8px",
              fontSize: 10,
              fontWeight: 500,
              color: "var(--text-muted)",
              background: "transparent",
              border: "1px solid var(--border-medium)",
              borderRadius: 6,
              textDecoration: "none",
              lineHeight: 1.1,
            }}
          >
            <ExternalLink size={11} aria-hidden="true" /> Portal
          </a>
        )}
      </div>
      <div
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
