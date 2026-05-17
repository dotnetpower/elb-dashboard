import { useQuery } from "@tanstack/react-query";

import type { BlastDatabase } from "@/api/blast";
import type { WarmupDbInfo } from "@/api/endpoints";
import { blastApi, monitoringApi } from "@/api/endpoints";

import type { DbChip } from "./types";

/**
 * Fetches the per-DB warmup status (k8s job state) and the per-DB sharded
 * layouts (storage listing) for a single cluster, then merges them into a
 * single ordered chip list. Every DB the platform "knows about" gets one
 * chip; badges accumulate (warmed / sharded).
 *
 * Cluster topology is forwarded so the backend can attach a `warmup_plan`
 * verdict to each DB row (Phase 1 of the warmup pipeline).
 */
export function useClusterDbChips(args: {
  subscriptionId: string;
  resourceGroup: string;
  clusterName: string;
  isRunning: boolean;
  isTransitioning: boolean;
  storageAccount?: string;
  storageResourceGroup?: string;
  clusterNumNodes: number;
  clusterMachineType: string;
}) {
  const {
    subscriptionId,
    resourceGroup,
    clusterName,
    isRunning,
    isTransitioning,
    storageAccount,
    storageResourceGroup,
    clusterNumNodes,
    clusterMachineType,
  } = args;

  const warmupQuery = useQuery({
    queryKey: ["warmup-status", subscriptionId, resourceGroup, clusterName],
    queryFn: () =>
      monitoringApi.warmupStatus(subscriptionId, resourceGroup, clusterName),
    enabled: isRunning && !isTransitioning,
    staleTime: 30_000,
    refetchInterval: isRunning ? 60_000 : false,
    retry: 1,
  });
  const warmupDbs: WarmupDbInfo[] = warmupQuery.data?.databases ?? [];
  const isWarm = warmupQuery.data?.warm ?? false;

  const dbListQuery = useQuery({
    queryKey: [
      "blast-databases-with-plan",
      subscriptionId,
      storageAccount ?? "",
      storageResourceGroup ?? "",
      clusterNumNodes,
      clusterMachineType,
    ],
    queryFn: () =>
      blastApi.listDatabases(
        subscriptionId,
        storageAccount as string,
        storageResourceGroup as string,
        clusterNumNodes > 0 && clusterMachineType
          ? { numNodes: clusterNumNodes, machineType: clusterMachineType }
          : undefined,
      ),
    enabled: isRunning && !!storageAccount && !!storageResourceGroup,
    staleTime: 60_000,
    retry: 0,
    // Tighten the poll cadence while any DB is mid-shard so the chip
    // strip flips state quickly when the daemon (or the auto-shard
    // step inside warmup) finishes. Falls back to no auto-refetch
    // otherwise — the staleTime invalidate covers normal refresh.
    refetchInterval: (query) => {
      const databases = (query.state.data as { databases?: BlastDatabase[] } | undefined)
        ?.databases;
      const anyInFlight = databases?.some((d) => d.sharding_in_progress) ?? false;
      return anyInFlight ? 5_000 : false;
    },
  });
  const dbListDegraded =
    (dbListQuery.data as unknown as { degraded?: boolean })?.degraded === true;
  const databasesInStorage = dbListQuery.data?.databases ?? [];

  const dbChips: DbChip[] = (() => {
    const byName = new Map<string, DbChip>();
    for (const db of databasesInStorage) {
      byName.set(db.name, {
        name: db.name,
        sharded: !!db.sharded && (db.shard_sets?.length ?? 0) > 0,
        shardLayouts: db.shard_sets?.length ?? 0,
        shardingInProgress: !!db.sharding_in_progress,
        shardingError: db.sharding_error ?? null,
        warmupPlan: db.warmup_plan,
      });
    }
    for (const w of warmupDbs) {
      const existing = byName.get(w.name);
      if (existing) existing.warm = w;
      else
        byName.set(w.name, {
          name: w.name,
          warm: w,
          sharded: false,
          shardLayouts: 0,
          shardingInProgress: false,
          shardingError: null,
        });
    }
    return Array.from(byName.values()).sort((a, b) => a.name.localeCompare(b.name));
  })();

  // Phase 1 warmup feasibility — surface a banner when at least one DB
  // would refuse warmup on the current cluster topology. The planner
  // status `ok` and `ok_unknown_sku` are silent (no banner). Anything
  // else gets called out so the user does not click "Warmup" and wait
  // for it to fail at the DaemonSet stage.
  const infeasibleDbs = dbChips.filter(
    (d) =>
      d.warmupPlan != null &&
      d.warmupPlan.feasible === false &&
      d.warmupPlan.status !== "no_db_size" &&
      d.warmupPlan.status !== "no_nodes",
  );

  return {
    warmupQuery,
    warmupDbs,
    isWarm,
    dbChips,
    infeasibleDbs,
    dbListDegraded,
  };
}
