import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import { monitoringApi } from "@/api/endpoints";

import { isSystemPool } from "./k8sFormat";

export type NodeSummary = {
  total: number;
  systemCount: number;
  userCount: number;
  cpuUsedM: number;
  cpuTotalM: number;
  memUsedKi: number;
  memTotalKi: number;
  cpuPct: number;
  memPct: number;
  notReady: number;
  hot: number;
  pressure: string[];
};

/**
 * Direct-K8s metrics summary (~1-3s fetch instead of ~30s ARM Run Command).
 * Aggregates per-node metrics into a single compact row for the card body;
 * the full per-node breakdown lives in the modal so the dashboard does not
 * render the same data twice.
 */
export function useNodeSummary(args: {
  subscriptionId: string;
  resourceGroup: string;
  clusterName: string;
  isRunning: boolean;
}) {
  const { subscriptionId, resourceGroup, clusterName, isRunning } = args;
  const topQuery = useQuery({
    queryKey: ["aks-top-fast", subscriptionId, resourceGroup, clusterName],
    queryFn: () => monitoringApi.k8sTopNodes(subscriptionId, resourceGroup, clusterName),
    enabled: isRunning,
    staleTime: 30_000,
    retry: 1,
    refetchInterval: isRunning ? 60_000 : false,
  });

  const summary: NodeSummary = useMemo(() => {
    const nodes = topQuery.data?.nodes ?? [];
    let cpuUsedM = 0;
    let cpuTotalM = 0;
    let memUsedKi = 0;
    let memTotalKi = 0;
    let systemCount = 0;
    let userCount = 0;
    let notReady = 0;
    let hot = 0;
    const pressureFlags = new Set<string>();
    for (const n of nodes) {
      cpuUsedM += n.cpu_m ?? 0;
      cpuTotalM += n.cpu_capacity_m ?? 0;
      memUsedKi += n.mem_ki ?? 0;
      memTotalKi += n.mem_capacity_ki ?? 0;
      if (isSystemPool(n.pool)) systemCount += 1;
      else userCount += 1;
      if (n.ready === false) notReady += 1;
      if (n.cpu_pct > 80 || n.memory_pct > 80) hot += 1;
      const conds = n.conditions ?? {};
      if (conds.MemoryPressure === "True") pressureFlags.add("MemoryPressure");
      if (conds.DiskPressure === "True") pressureFlags.add("DiskPressure");
      if (conds.PIDPressure === "True") pressureFlags.add("PIDPressure");
    }
    const cpuPct = cpuTotalM > 0 ? Math.round((cpuUsedM / cpuTotalM) * 1000) / 10 : 0;
    const memPct =
      memTotalKi > 0 ? Math.round((memUsedKi / memTotalKi) * 1000) / 10 : 0;
    return {
      total: nodes.length,
      systemCount,
      userCount,
      cpuUsedM,
      cpuTotalM,
      memUsedKi,
      memTotalKi,
      cpuPct,
      memPct,
      notReady,
      hot,
      pressure: [...pressureFlags],
    };
  }, [topQuery.data]);

  return { topQuery, summary };
}
