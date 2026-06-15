/**
 * Readiness-state fallback for {@link ClusterBento}.
 *
 * Rendered in place of the live "Mission Control Bento" whenever the cluster
 * is not workload-ready — i.e. it is starting, stopping, stopped, or still
 * provisioning. It is a distinct responsibility from the live-metrics layout
 * (no data hooks, no degraded cells; just a calm "what's happening / what's
 * next" panel + a static cluster summary), so it lives in its own module.
 *
 * Driven entirely by the `AksClusterSummary` the parent already holds plus the
 * optional `transition` hint — no additional data fetching.
 */

import { Activity, Box, Cpu, Database, Layers, Loader2, PlayCircle } from "lucide-react";

import type { AksClusterSummary } from "@/api/endpoints";
import { getAksProvisioningLabel, isAksProvisioning } from "@/utils/aksStatus";

import { BentoCell, Eyebrow } from "./atoms";
import {
  SummaryRow,
  emptyNodeSummary,
  topologyPoolsLabel,
} from "./clusterSummaryHelpers";

export function ClusterReadinessBento({
  cluster,
  transition,
}: {
  cluster: AksClusterSummary;
  transition?: "starting" | "stopping";
}) {
  const provisioningLabel = getAksProvisioningLabel(cluster);
  const isStarting =
    transition === "starting" ||
    provisioningLabel === "Starting" ||
    cluster.power_state === "Starting";
  const isStopping = transition === "stopping";
  const isProvisioning = isAksProvisioning(cluster);
  const title = isStarting
    ? "Cluster is starting"
    : isStopping
      ? "Cluster is stopping"
      : cluster.power_state === "Stopped"
        ? "Cluster is stopped"
        : isProvisioning
          ? "Cluster is provisioning"
          : "Cluster is not workload-ready";
  const body = isStarting
    ? "AKS is coming online. Submit metrics, node activity, and warm-cache controls appear after the workload nodes report Running."
    : isStopping
      ? "AKS is shutting down. Live workload metrics are paused until the next start completes."
      : cluster.power_state === "Stopped"
        ? "Start the cluster to enable submit monitoring, node metrics, and automatic warmup."
        : "The control plane can see this cluster, but workload checks are not ready yet.";
  const nextStep = isStarting
    ? "Auto warm will be reconciled by Celery after the cluster becomes ready."
    : isStopping
      ? "Queued Celery work remains tracked while the browser can be refreshed safely."
      : cluster.power_state === "Stopped"
        ? "Use Start on the cluster header when you are ready to run BLAST jobs."
        : "Keep this view open or refresh later; the dashboard will update automatically.";

  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "minmax(0, 1.4fr) minmax(260px, 0.8fr)",
        gap: 10,
      }}
    >
      <BentoCell span={[1, 1]} accent={isStarting ? "var(--accent)" : "var(--warning)"}>
        <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
          <div
            style={{
              width: 34,
              height: 34,
              borderRadius: 8,
              display: "grid",
              placeItems: "center",
              background: isStarting
                ? "rgba(122, 167, 255, 0.12)"
                : "rgba(240, 198, 116, 0.10)",
              color: isStarting ? "var(--accent)" : "var(--warning)",
              flexShrink: 0,
            }}
          >
            {isStarting || isStopping || isAksProvisioning(cluster) ? (
              <Loader2 size={17} className="spin" />
            ) : (
              <PlayCircle size={17} />
            )}
          </div>
          <div style={{ minWidth: 0, display: "flex", flexDirection: "column", gap: 8 }}>
            <div
              style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}
            >
              <div
                style={{ fontSize: 16, fontWeight: 650, color: "var(--text-primary)" }}
              >
                {title}
              </div>
              <ReadinessPill
                label={
                  isStarting
                    ? "Starting"
                    : isStopping
                      ? "Stopping"
                      : (provisioningLabel ?? cluster.power_state ?? "Waiting")
                }
                tone={
                  isStarting
                    ? "var(--accent)"
                    : isStopping
                      ? "var(--warning)"
                      : "var(--text-muted)"
                }
                spinning={isStarting || isStopping || isProvisioning}
              />
            </div>
            <div
              style={{
                fontSize: 12,
                lineHeight: 1.55,
                color: "var(--text-muted)",
                maxWidth: 680,
              }}
            >
              {body}
            </div>
            <div
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                width: "fit-content",
                padding: "6px 9px",
                border: "1px solid var(--border-weak)",
                borderRadius: 7,
                color: "var(--text-muted)",
                background: "rgba(255,255,255,0.025)",
                fontSize: 11,
              }}
            >
              <Activity size={11} color="var(--accent)" />
              {nextStep}
            </div>
          </div>
        </div>
      </BentoCell>

      <BentoCell span={[1, 1]}>
        <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 8 }}>
          <Layers size={11} color="var(--accent)" />
          <Eyebrow>Cluster summary</Eyebrow>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 7, fontSize: 11 }}>
          <SummaryRow
            icon={<Box size={11} />}
            label="Nodes"
            value={cluster.node_count?.toString() ?? "—"}
          />
          <SummaryRow
            icon={<Cpu size={11} />}
            label="SKU"
            value={cluster.node_sku ?? "—"}
          />
          <SummaryRow
            icon={<Database size={11} />}
            label="Pools"
            value={
              cluster.agent_pools?.length
                ? topologyPoolsLabel(cluster, emptyNodeSummary())
                : "—"
            }
          />
          <SummaryRow
            icon={<Activity size={11} />}
            label="K8s"
            value={cluster.k8s_version ?? "—"}
          />
        </div>
      </BentoCell>
    </div>
  );
}

function ReadinessPill({
  label,
  tone,
  spinning,
}: {
  label: string;
  tone: string;
  spinning: boolean;
}) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        padding: "3px 8px",
        borderRadius: 999,
        border: "1px solid var(--border-weak)",
        color: tone,
        background: "rgba(255,255,255,0.03)",
        fontSize: 10,
        fontWeight: 650,
        letterSpacing: "0.03em",
        textTransform: "uppercase",
      }}
    >
      {spinning && <Loader2 size={10} className="spin" />}
      {label}
    </span>
  );
}
