import type { AksAgentPool, AksClusterSummary } from "@/api/endpoints";
import { normalizeSkuName } from "@/utils/dbSharding";

const BLAST_POOL_NAME = "blastpool";

export function selectWorkloadPool(cluster: AksClusterSummary): AksAgentPool | undefined {
  const pools = cluster.agent_pools ?? [];
  return (
    pools.find((pool) => pool.name.toLowerCase() === BLAST_POOL_NAME) ??
    pools.find((pool) => (pool.mode ?? "").toLowerCase() === "user")
  );
}

export function getWorkloadNodeSku(cluster: AksClusterSummary): string | null {
  const sku = selectWorkloadPool(cluster)?.vm_size ?? cluster.node_sku;
  return normalizeSkuName(sku) || null;
}

export function getWorkloadNodeCount(cluster: AksClusterSummary): number | null {
  return selectWorkloadPool(cluster)?.count ?? cluster.node_count;
}