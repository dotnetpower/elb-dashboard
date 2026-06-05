import { useMemo } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

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

  const queryClient = useQueryClient();

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
    // The catalogue changes rarely and the backend now serves it from an
    // event-invalidated cache, so keep the listing fresh for two minutes and
    // retain it for half an hour. This lets a Dashboard prefetch (and a
    // re-visit / cluster switch) reuse the cache without re-showing the
    // "Choose Search Set" skeleton.
    staleTime: 120_000,
    gcTime: 30 * 60_000,
    // When the cluster picker resolves, the query key flips from the
    // topology-free shape (numNodes=0, sku="") to the topology-scoped shape.
    // That new key has no client-cache entry yet, which would re-show the
    // picker skeleton on first visit. Seed it with the topology-free base
    // listing (prefetched on the Dashboard / nav hover, or fetched on this
    // page's first render before the cluster resolved) so the picker renders
    // immediately while the warmup_plan-enriched rows load in the background.
    // Only the per-row ``warmup_plan`` differs between the two shapes, and it
    // degrades gracefully when momentarily absent.
    placeholderData: () => {
      if (!(clusterNodeCount > 0 && clusterNodeSku)) return undefined;
      return queryClient.getQueryData<{ databases: BlastDatabase[] }>([
        "blast-databases",
        subId,
        storageAccount,
        0,
        "",
      ]);
    },
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
