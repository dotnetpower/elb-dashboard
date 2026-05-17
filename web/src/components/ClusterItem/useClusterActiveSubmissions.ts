import { useQuery } from "@tanstack/react-query";

import { blastApi } from "@/api/endpoints";

const ACTIVE_PHASES = new Set([
  "Provisioning",
  "DownloadingDB",
  "Splitting",
  "Running",
  "Submitted",
  "InProgress",
  "Pending",
]);

type ActiveSubmissionRow = {
  status?: string;
  phase?: string;
  infrastructure?: { cluster_name?: string };
  payload?: { cluster_name?: string };
};

export type ActiveSubmission = { phase?: string };

/**
 * Active BLAST submissions for one cluster. The dashboard's own
 * `/api/blast/jobs` returns either {jobs: [...]} (real or empty list) or
 * {jobs: [], degraded: true} when the state-store table isn't configured.
 * We treat "degraded" as "tracking unavailable" so callers can render the
 * runtime line accordingly (static capacity vs. "is my submit done?").
 */
export function useClusterActiveSubmissions(args: {
  clusterName: string;
  isRunning: boolean;
  isTransitioning: boolean;
}) {
  const { clusterName, isRunning, isTransitioning } = args;

  const blastJobsQuery = useQuery({
    queryKey: ["blast-jobs-for-cluster", clusterName],
    queryFn: () => blastApi.listJobs(),
    enabled: isRunning && !isTransitioning,
    staleTime: 30_000,
    refetchInterval: isRunning ? 60_000 : false,
    retry: 0,
  });

  const tracking =
    blastJobsQuery.data != null &&
    !(blastJobsQuery.data as unknown as { degraded?: boolean }).degraded;

  const submissions: ActiveSubmission[] = (() => {
    const rows = (blastJobsQuery.data?.jobs ?? []) as unknown as ActiveSubmissionRow[];
    return rows.filter((r) => {
      const cluster =
        r.infrastructure?.cluster_name ?? r.payload?.cluster_name ?? null;
      if (cluster && cluster !== clusterName) return false;
      const phase = r.phase ?? r.status ?? "";
      return ACTIVE_PHASES.has(phase);
    });
  })();

  return { tracking, submissions };
}
