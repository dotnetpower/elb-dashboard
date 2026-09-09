import type {
  AksClusterSummary,
  BlastRuntimeEstimateRequest,
} from "@/api/endpoints";
import { parseFasta } from "@/pages/blastSubmit/fastaUtils";
import type { BlastProgram } from "@/api/blast.types";

interface RuntimeEstimateInputArgs {
  subscriptionId: string;
  program: BlastProgram;
  database: { name?: string; total_letters?: number | null } | null | undefined;
  queryData: string;
  cluster: AksClusterSummary | null | undefined;
}

export function fastaLetterCount(fasta: string): number {
  return parseFasta(fasta).reduce((total, record) => total + record.sequence.length, 0);
}

export function buildRuntimeEstimateInput({
  subscriptionId,
  program,
  database,
  queryData,
  cluster,
}: RuntimeEstimateInputArgs): BlastRuntimeEstimateRequest | null {
  if (!subscriptionId || !database?.name || !database.total_letters || !cluster) return null;
  const queryLetters = fastaLetterCount(queryData);
  if (queryLetters <= 0) return null;

  const workloadPool = cluster.agent_pools?.find(
    (pool) =>
      pool.name.toLowerCase().includes("blast") ||
      (pool.mode ?? "").toLowerCase() !== "system",
  );
  const nodeCount = workloadPool ? workloadPool.count : cluster.node_count;
  const nodeSku = workloadPool ? workloadPool.vm_size : cluster.node_sku;
  if (!nodeCount || nodeCount <= 0 || !nodeSku) return null;

  return {
    subscription_id: subscriptionId,
    resource_group: cluster.resource_group,
    cluster_name: cluster.name,
    program,
    database: database.name,
    query_letters: queryLetters,
    database_letters: database.total_letters,
    node_count: nodeCount,
    node_sku: nodeSku,
    region: cluster.region,
  };
}