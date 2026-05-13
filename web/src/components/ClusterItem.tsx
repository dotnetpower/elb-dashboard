import { useState } from "react";
import {
  Loader2,
  Play,
  Square,
  Copy,
  ChevronDown,
  Trash2,
  Flame,
} from "lucide-react";
import { useQuery } from "@tanstack/react-query";

import type { AksClusterSummary, WarmupDbInfo } from "@/api/endpoints";
import { monitoringApi } from "@/api/endpoints";
import { ClusterDetails } from "@/components/ClusterDetailModal";

const CLUSTER_COLLAPSED_KEY = "elb-cluster-collapsed-";

// ClusterItem — collapsible per-cluster card (stopped clusters collapsed by default)
// ---------------------------------------------------------------------------

export function ClusterItem({
  cluster: c,
  transitioning,
  actionLoading,
  onStartStop,
  onDelete,
  subscriptionId,
  resourceGroup,
  storageAccount,
  storageResourceGroup,
  acrResourceGroup,
  acrName,
  region,
}: {
  cluster: AksClusterSummary;
  transitioning: Map<string, "starting" | "stopping">;
  actionLoading: string | null;
  onStartStop: (name: string, action: "start" | "stop") => void;
  onDelete: (name: string) => void;
  subscriptionId: string;
  resourceGroup: string;
  storageAccount?: string;
  storageResourceGroup?: string;
  acrResourceGroup?: string;
  acrName?: string;
  region?: string;
}) {
  const isStopped = c.power_state === "Stopped";
  const isRunning = c.power_state === "Running";
  const [collapsed, setCollapsed] = useState(() => {
    try {
      const v = localStorage.getItem(CLUSTER_COLLAPSED_KEY + c.name);
      return v != null ? v === "1" : isStopped; // Stopped clusters collapsed by default
    } catch {
      return isStopped;
    }
  });

  const toggleCollapse = () => {
    setCollapsed((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(CLUSTER_COLLAPSED_KEY + c.name, next ? "1" : "0");
      } catch {
        /* noop */
      }
      return next;
    });
  };

  // Warmup status — only poll when cluster is running
  const warmupQuery = useQuery({
    queryKey: ["warmup-status", subscriptionId, resourceGroup, c.name],
    queryFn: () => monitoringApi.warmupStatus(subscriptionId, resourceGroup, c.name),
    enabled: isRunning && !transitioning.has(c.name),
    staleTime: 30_000,
    refetchInterval: isRunning ? 60_000 : false,
    retry: 1,
  });
  const warmupDbs: WarmupDbInfo[] = warmupQuery.data?.databases ?? [];
  const isWarm = warmupQuery.data?.warm ?? false;

  const trans = transitioning.get(c.name);
  const powerLabel =
    trans === "starting"
      ? "Starting..."
      : trans === "stopping"
        ? "Stopping..."
        : (c.power_state ?? "?");
  const powerColor =
    trans === "starting"
      ? "var(--accent)"
      : trans === "stopping"
        ? "var(--warning)"
        : c.power_state === "Running"
          ? "var(--success)"
          : "var(--warning)";

  return (
    <li className="glass-card" style={{ padding: "var(--space-3)" }}>
      {/* Row 1: name + status + actions */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          cursor: "pointer",
          flexWrap: "wrap",
        }}
        onClick={toggleCollapse}
      >
        <ChevronDown
          size={14}
          style={{
            transform: collapsed ? "rotate(-90deg)" : "rotate(0)",
            transition: "transform 0.15s",
            color: "var(--text-faint)",
            flexShrink: 0,
          }}
        />
        <strong
          style={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}
        >
          {c.name}
        </strong>
        <span
          style={{ fontSize: 11, color: powerColor, fontWeight: 600, flexShrink: 0 }}
        >
          {(trans === "starting" || trans === "stopping") && (
            <Loader2
              size={10}
              className="spin"
              style={{ verticalAlign: "middle", marginRight: 3 }}
            />
          )}
          {powerLabel}
        </span>
        <div
          style={{
            display: "flex",
            gap: "var(--space-2)",
            alignItems: "center",
            flexShrink: 0,
          }}
          onClick={(e) => e.stopPropagation()}
        >
          {!trans && c.power_state === "Stopped" && (
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
          {!trans && c.power_state === "Running" && (
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
      {/* Row 2: metadata chips — always visible */}
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: "6px 10px",
          marginTop: 4,
          marginLeft: 22,
          fontSize: 11,
          color: "var(--text-muted)",
        }}
      >
        <span>· {c.node_count ?? "?"} nodes</span>
        <span>({c.node_sku ?? "?"})</span>
        <span>· {c.region}</span>
        <span>· {c.k8s_version ?? "?"}</span>
      </div>

      {/* Warmup badges — always visible when warm */}
      {isRunning && warmupDbs.length > 0 && (
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: 6,
            marginTop: 4,
            marginLeft: 22,
          }}
        >
          {warmupDbs.map((db) => (
            <span
              key={db.name}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 3,
                fontSize: 10,
                padding: "2px 8px",
                borderRadius: 10,
                background:
                  db.status === "Ready"
                    ? "rgba(106,214,163,0.12)"
                    : db.status === "Loading"
                      ? "rgba(122,167,255,0.12)"
                      : "rgba(224,123,138,0.12)",
                color:
                  db.status === "Ready"
                    ? "var(--success)"
                    : db.status === "Loading"
                      ? "var(--accent)"
                      : "var(--danger)",
                border: `1px solid ${
                  db.status === "Ready"
                    ? "rgba(106,214,163,0.25)"
                    : db.status === "Loading"
                      ? "rgba(122,167,255,0.25)"
                      : "rgba(224,123,138,0.25)"
                }`,
              }}
              title={`${db.name}: ${db.nodes_ready}/${db.total_jobs} nodes ready`}
            >
              {db.status === "Loading" ? (
                <Loader2 size={9} className="spin" />
              ) : (
                <Flame size={9} strokeWidth={1.5} />
              )}
              {db.name}
              {db.status === "Ready" && (
                <span style={{ opacity: 0.7 }}>
                  {db.nodes_ready}/{db.total_jobs}
                </span>
              )}
              {db.status === "Loading" && (
                <span style={{ opacity: 0.7 }}>
                  {db.nodes_ready}/{db.total_jobs}
                </span>
              )}
            </span>
          ))}
        </div>
      )}
      {isRunning && isWarm && warmupDbs.length === 0 && (
        <div
          style={{
            display: "flex",
            gap: 4,
            marginTop: 4,
            marginLeft: 22,
            fontSize: 10,
            color: "var(--success)",
          }}
        >
          <Flame size={10} strokeWidth={1.5} /> Workspace ready
        </div>
      )}

      {!collapsed && (
        <>
          <div className="muted" style={{ fontSize: 11, marginTop: 4, marginLeft: 22 }}>
            State:{" "}
            {(() => {
              const ps = c.provisioning_state ?? "?";
              if (ps === "Succeeded")
                return <span style={{ color: "var(--success)" }}>{ps}</span>;
              if (ps === "Creating" || ps === "Updating")
                return (
                  <span
                    style={{
                      color: "var(--accent)",
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 3,
                    }}
                  >
                    <Loader2 size={10} className="spin" />
                    {ps}
                  </span>
                );
              if (ps === "Deleting")
                return (
                  <span
                    style={{
                      color: "var(--warning)",
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 3,
                    }}
                  >
                    <Loader2 size={10} className="spin" />
                    {ps}
                  </span>
                );
              if (ps === "Failed")
                return <span style={{ color: "var(--danger)" }}>{ps}</span>;
              return <span>{ps}</span>;
            })()}
          </div>
          {c.kubelet_object_id && (
            <div
              className="muted"
              style={{
                fontSize: 11,
                marginTop: 2,
                marginLeft: 22,
                display: "flex",
                alignItems: "center",
                gap: 4,
              }}
            >
              Kubelet OID: <code style={{ fontSize: 10 }}>{c.kubelet_object_id}</code>
              <button
                className="glass-button"
                style={{ padding: "1px 4px", border: "none", opacity: 0.6 }}
                onClick={() => navigator.clipboard.writeText(c.kubelet_object_id!)}
                title="Copy OID"
              >
                <Copy size={9} />
              </button>
            </div>
          )}
          <ClusterDetails
            clusterName={c.name}
            powerState={c.power_state}
            isTransitioning={!!trans}
            agentPools={c.agent_pools}
            fqdn={c.fqdn}
            networkPlugin={c.network_plugin}
            subscriptionId={subscriptionId}
            resourceGroup={resourceGroup}
            warmupDbs={warmupDbs}
            warmupQuery={warmupQuery}
            storageAccount={storageAccount}
            storageResourceGroup={storageResourceGroup}
            acrResourceGroup={acrResourceGroup}
            acrName={acrName}
            region={region}
          />
        </>
      )}
    </li>
  );
}

// ---------------------------------------------------------------------------
// Cluster Details — compact inline summary + modal for full details
// ---------------------------------------------------------------------------
