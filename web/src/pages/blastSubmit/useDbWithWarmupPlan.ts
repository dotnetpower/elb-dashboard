import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import type { AksClusterSummary } from "@/api/endpoints";
import { blastApi } from "@/api/endpoints";
import type { BlastDatabase, BlastWarmupPlan } from "@/api/blast";
import {
  getWorkloadNodeCount,
  getWorkloadNodeSku,
} from "@/pages/blastSubmit/computeEnvironment";

interface UseDbWithWarmupPlanOptions {
  subId: string;
  storageAccount: string;
  workloadRg: string;
  selectedCluster?: AksClusterSummary;
  selectedDbShortName: string;
  warmupRequested: boolean;
}

interface UseDbWithWarmupPlanResult {
  /** Raw TanStack Query handle — exposed for consumers that need
   *  ``isLoading`` / ``isSuccess`` / ``data`` directly. */
  dbQuery: ReturnType<typeof useQuery<{ databases: BlastDatabase[] }>>;
  /** Convenience accessor for the database list (never undefined). */
  databases: BlastDatabase[];
  /** Database row matching ``selectedDbShortName`` (or undefined). */
  selectedDbInfo?: BlastDatabase;
  /** Server-computed warmup feasibility for the selected DB on the
   *  selected cluster. ``undefined`` whenever the request was made
   *  without cluster topology, or when no DB is selected. */
  selectedDbPlan?: BlastWarmupPlan;
  /** True when warmup is requested AND the planner says it cannot run
   *  on this cluster. The submit button must be blocked in this case. */
  warmupBlocked: boolean;
}

/**
 * Single-responsibility hook that owns the BLAST database listing for the
 * Submit page, including the warmup-feasibility plan returned when cluster
 * topology is supplied.
 *
 * Responsibilities:
 * - Derive cluster topology (node count + SKU) from the selected cluster.
 * - Issue ``GET /api/blast/databases`` keyed by topology so a cluster
 *   switch triggers an automatic refetch (and isolated cache entry).
 * - Memoise the selected DB row + its server-computed warmup plan.
 * - Compute ``warmupBlocked`` — the single boolean the parent page reads
 *   to gate ``canSubmit``. Defence-in-depth in ``handleSubmit`` checks
 *   the same value.
 *
 * What this hook deliberately does NOT do:
 * - It does not own the warmup-status (kubelet) query — that lives in
 *   ``BlastSubmit`` because it pulls from a different endpoint and has
 *   its own polling cadence.
 * - It does not render anything — pure state derivation.
 */
export function useDbWithWarmupPlan({
  subId,
  storageAccount,
  workloadRg,
  selectedCluster,
  selectedDbShortName,
  warmupRequested,
}: UseDbWithWarmupPlanOptions): UseDbWithWarmupPlanResult {
  // Cluster topology — only attached to the request when both values are
  // known. Otherwise the backend skips the warmup_plan enrichment and
  // each row degrades gracefully.
  const clusterNodeCount = selectedCluster
    ? getWorkloadNodeCount(selectedCluster) ?? 0
    : 0;
  const clusterNodeSku = selectedCluster
    ? getWorkloadNodeSku(selectedCluster) ?? ""
    : "";

  const dbQuery = useQuery({
    queryKey: [
      "blast-databases",
      subId,
      storageAccount,
      clusterNodeCount,
      clusterNodeSku,
    ],
    queryFn: () =>
      blastApi.listDatabases(
        subId,
        storageAccount,
        workloadRg,
        clusterNodeCount > 0 && clusterNodeSku
          ? { numNodes: clusterNodeCount, machineType: clusterNodeSku }
          : undefined,
      ),
    enabled: Boolean(subId && storageAccount && workloadRg),
  });

  const databases = useMemo(
    () => dbQuery.data?.databases ?? [],
    [dbQuery.data?.databases],
  );

  const selectedDbInfo = useMemo(
    () => databases.find((d) => d.name === selectedDbShortName),
    [databases, selectedDbShortName],
  );

  const selectedDbPlan = selectedDbInfo?.warmup_plan;

  const warmupBlocked =
    warmupRequested &&
    selectedDbPlan != null &&
    selectedDbPlan.feasible === false;

  return { dbQuery, databases, selectedDbInfo, selectedDbPlan, warmupBlocked };
}
