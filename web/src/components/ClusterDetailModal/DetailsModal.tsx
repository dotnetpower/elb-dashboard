import { useEffect } from "react";
import { createPortal } from "react-dom";
import { Loader2 } from "lucide-react";
import type { UseQueryResult } from "@tanstack/react-query";

import type {
  AksAgentPool,
  WarmupDbInfo,
  WarmupStatus,
} from "@/api/endpoints";
import { monitoringApi } from "@/api/endpoints";
import { ClusterModalKubectl } from "@/components/ClusterDiagnostics";
import { WarmupSection } from "@/components/WarmupSection";

import { IdentitySection } from "./IdentitySection";
import { ModalHeader } from "./ModalHeader";
import { NodePoolsTable } from "./NodePoolsTable";

type TopNodesResponse = Awaited<ReturnType<typeof monitoringApi.k8sTopNodes>>;

export function DetailsModal({
  clusterName,
  powerState,
  isTransitioning,
  agentPools,
  fqdn,
  networkPlugin,
  subscriptionId,
  resourceGroup,
  warmupDbs,
  warmupQuery,
  storageAccount,
  storageResourceGroup,
  acrResourceGroup,
  acrName,
  region,
  nodeSku,
  nodeCount,
  terminalResourceGroup,
  terminalVmName,
  kubeletObjectId,
  topQuery,
  onClose,
}: {
  clusterName: string;
  powerState: string | null;
  isTransitioning: boolean;
  agentPools?: AksAgentPool[];
  fqdn?: string | null;
  networkPlugin?: string | null;
  subscriptionId: string;
  resourceGroup: string;
  warmupDbs?: WarmupDbInfo[];
  warmupQuery?: UseQueryResult<WarmupStatus>;
  storageAccount?: string;
  storageResourceGroup?: string;
  acrResourceGroup?: string;
  acrName?: string;
  region?: string;
  nodeSku?: string | null;
  nodeCount?: number | null;
  terminalResourceGroup?: string;
  terminalVmName?: string;
  kubeletObjectId?: string | null;
  topQuery: UseQueryResult<TopNodesResponse>;
  onClose: () => void;
}) {
  const isRunning = powerState === "Running" && !isTransitioning;

  // ESC + body scroll lock
  useEffect(() => {
    const handleEsc = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleEsc);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", handleEsc);
      document.body.style.overflow = prev;
    };
  }, [onClose]);

  return createPortal(
    <div
      className="glass-dialog-backdrop"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      role="dialog"
      aria-modal="true"
      aria-label={`${clusterName} Details`}
    >
      <div
        className="glass-card glass-card--strong glass-dialog"
        onClick={(e) => e.stopPropagation()}
        style={{
          maxWidth: 1180,
          width: "calc(100vw - 48px)",
          maxHeight: "92vh",
          display: "flex",
          flexDirection: "column",
          padding: 0,
          overflow: "hidden",
        }}
      >
        <ModalHeader
          clusterName={clusterName}
          powerState={powerState}
          fqdn={fqdn}
          agentPools={agentPools}
          networkPlugin={networkPlugin}
          onClose={onClose}
        />

        {/* Scrollable body */}
        <div style={{ overflowY: "auto", flex: 1, padding: "16px 24px 24px" }}>
          {kubeletObjectId && <IdentitySection kubeletObjectId={kubeletObjectId} />}

          {agentPools && agentPools.length > 0 && (
            <NodePoolsTable agentPools={agentPools} />
          )}

          {/* kubectl sections — only when fully running */}
          {!isRunning && (
            <div
              style={{
                padding: "24px 20px",
                borderRadius: 8,
                textAlign: "center",
                background: "rgba(255,255,255,0.02)",
                border: "1px dashed var(--border-weak)",
                color: "var(--text-faint)",
                fontSize: 12,
                display: "flex",
                flexDirection: "column",
                alignItems: "center",
                gap: 8,
              }}
            >
              {isTransitioning ||
              (powerState !== "Stopped" && powerState !== "Running") ? (
                <>
                  <Loader2
                    size={18}
                    className="spin"
                    style={{ color: "var(--accent)" }}
                  />
                  <span>
                    Cluster is <strong>{powerState ?? "transitioning"}</strong> —
                    diagnostics will be available once it finishes starting.
                  </span>
                </>
              ) : (
                <span>
                  Start the cluster to view diagnostics and run kubectl commands.
                </span>
              )}
            </div>
          )}

          {isRunning && (
            <ClusterModalKubectl
              subscriptionId={subscriptionId}
              resourceGroup={resourceGroup}
              clusterName={clusterName}
              topQuery={topQuery}
            />
          )}

          {/* Warmup section — DB cache management */}
          {isRunning && (
            <WarmupSection
              subscriptionId={subscriptionId}
              resourceGroup={resourceGroup}
              clusterName={clusterName}
              warmupDbs={warmupDbs}
              warmupQuery={warmupQuery}
              storageAccount={storageAccount}
              storageResourceGroup={storageResourceGroup}
              acrResourceGroup={acrResourceGroup}
              acrName={acrName}
              region={region}
              nodeSku={nodeSku}
              nodeCount={nodeCount}
              nodeMetrics={topQuery.data?.nodes ?? []}
              terminalResourceGroup={terminalResourceGroup}
              terminalVmName={terminalVmName}
            />
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}
