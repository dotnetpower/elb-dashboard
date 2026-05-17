import { useState } from "react";
import { Loader2 } from "lucide-react";
import type { UseQueryResult } from "@tanstack/react-query";

import type {
  AksAgentPool,
  WarmupDbInfo,
  WarmupStatus,
} from "@/api/endpoints";

import { CompactNodeSummary } from "./CompactNodeSummary";
import { DetailsModal } from "./DetailsModal";
import { useNodeSummary } from "./useNodeSummary";

export function ClusterDetails({
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
}) {
  const isRunning = powerState === "Running" && !isTransitioning;
  const [showModal, setShowModal] = useState(false);

  const { topQuery, summary } = useNodeSummary({
    subscriptionId,
    resourceGroup,
    clusterName,
    isRunning,
  });

  return (
    <div style={{ marginTop: "var(--space-2)" }}>
      {/* One-line aggregate summary strip — opens the full modal on click. */}
      {isRunning && summary.total > 0 && (
        <CompactNodeSummary
          summary={summary}
          isFetching={topQuery.isFetching}
          onOpenModal={() => setShowModal(true)}
        />
      )}

      {isRunning && topQuery.isLoading && summary.total === 0 && (
        <div
          className="muted"
          style={{
            fontSize: 10,
            marginTop: 4,
            display: "flex",
            alignItems: "center",
            gap: 4,
          }}
        >
          <Loader2 size={10} className="spin" /> Loading node metrics...
        </div>
      )}

      {!isRunning && (
        <div className="muted" style={{ fontSize: 10, marginTop: 4 }}>
          Start the cluster to view node metrics.
        </div>
      )}

      {showModal && (
        <DetailsModal
          clusterName={clusterName}
          powerState={powerState}
          isTransitioning={isTransitioning}
          agentPools={agentPools}
          fqdn={fqdn}
          networkPlugin={networkPlugin}
          subscriptionId={subscriptionId}
          resourceGroup={resourceGroup}
          warmupDbs={warmupDbs}
          warmupQuery={warmupQuery}
          storageAccount={storageAccount}
          storageResourceGroup={storageResourceGroup}
          acrResourceGroup={acrResourceGroup}
          acrName={acrName}
          region={region}
          nodeSku={nodeSku}
          nodeCount={nodeCount}
          terminalResourceGroup={terminalResourceGroup}
          terminalVmName={terminalVmName}
          kubeletObjectId={kubeletObjectId}
          topQuery={topQuery}
          onClose={() => setShowModal(false)}
        />
      )}
    </div>
  );
}
