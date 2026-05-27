import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import { type AksClusterSummary, monitoringApi } from "@/api/endpoints";
import { isAksWorkloadReady } from "@/utils/aksStatus";

export interface UseWarmupStatusArgs {
  subId: string;
  workloadRg: string;
  selectedCluster: AksClusterSummary | undefined;
  formDb: string;
}

export function useWarmupStatus({
  subId,
  workloadRg,
  selectedCluster,
  formDb,
}: UseWarmupStatusArgs) {
  const warmupQuery = useQuery({
    queryKey: ["warmup-status-submit", subId, workloadRg, selectedCluster?.name],
    queryFn: () =>
      monitoringApi.warmupStatus(subId, workloadRg, selectedCluster!.name),
    enabled: Boolean(
      subId &&
        workloadRg &&
        selectedCluster?.name &&
        isAksWorkloadReady(selectedCluster),
    ),
    staleTime: 30_000,
  });

  const warmDbs = useMemo(() => {
    const dbs = warmupQuery.data?.databases ?? [];
    // Only treat a DB as "warm" when an explicit warmup Job/DaemonSet
    // contributed. `init-ssd-*` setup jobs from a prior BLAST submit also
    // cache the DB on node SSDs, but using their presence to auto-select
    // the "Warmed database" run profile confuses researchers who never
    // ran an explicit warmup. The `sources` discriminator is set by
    // `k8s_warmup_status` / `database_status_from_warmup_jobs`.
    return new Map(
      dbs
        .filter(
          (d) => d.status === "Ready" && (d.sources ?? []).includes("warmup"),
        )
        .map((d) => [d.name, d]),
    );
  }, [warmupQuery.data]);

  // Derive the short DB name from the form.db path
  // (e.g. "blast-db/core_nt" → "core_nt")
  const selectedDbShortName = useMemo(() => {
    if (!formDb) return "";
    const parts = formDb.split("/");
    return parts[parts.length - 1];
  }, [formDb]);

  const isDbAlreadyWarm = warmDbs.has(selectedDbShortName);
  const warmDbInfo = warmDbs.get(selectedDbShortName);

  return {
    warmupQuery,
    warmDbs,
    selectedDbShortName,
    isDbAlreadyWarm,
    warmDbInfo,
  } as const;
}
