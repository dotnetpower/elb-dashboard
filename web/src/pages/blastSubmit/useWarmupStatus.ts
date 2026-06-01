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
  // Derive the short DB name from the form.db path
  // (e.g. "blast-db/core_nt" → "core_nt"). Computed before the query so the
  // poll-stop heuristic can key off the currently selected database.
  const selectedDbShortName = useMemo(() => {
    if (!formDb) return "";
    const parts = formDb.split("/");
    return parts[parts.length - 1];
  }, [formDb]);

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
    // Poll while the cluster is ready but the selected DB is not yet confirmed
    // warm, so a warmup completing in the browser terminal or another tab is
    // reflected without a manual page reload. Stop polling once the selected
    // DB shows warm to avoid needless K8s warmup-status calls.
    refetchInterval: (query) => {
      const dbs = query.state.data?.databases ?? [];
      const selectedWarm = dbs.some(
        (d) =>
          d.name === selectedDbShortName &&
          d.status === "Ready" &&
          (d.sources ?? []).includes("warmup"),
      );
      return selectedWarm ? false : 20_000;
    },
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

  const isDbAlreadyWarm = warmDbs.has(selectedDbShortName);
  const warmDbInfo = warmDbs.get(selectedDbShortName);

  // Whether we have a trustworthy warm/not-warm answer for the selected
  // cluster yet. The query returns `undefined` while it is disabled (cluster
  // not workload-ready) or loading its first response; in those windows the
  // DB may actually be warm, so callers must not treat `!isDbAlreadyWarm` as
  // "confirmed cold". Once `data` is present (even a stale snapshot), the
  // answer is considered resolved.
  const isWarmupStatusResolved = warmupQuery.data !== undefined;

  return {
    warmupQuery,
    warmDbs,
    selectedDbShortName,
    isDbAlreadyWarm,
    isWarmupStatusResolved,
    warmDbInfo,
  } as const;
}
