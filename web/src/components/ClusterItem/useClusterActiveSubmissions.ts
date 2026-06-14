import type { BlastJobSummary } from "@/api/endpoints";
import {
  isDashboardJobActive,
  jobDisplayState,
} from "@/components/cards/ClusterBento/jobMapping";
import {
  blastJobsRefetchInterval,
  useScopedBlastJobs,
} from "@/hooks/useScopedBlastJobs";

type ActiveSubmissionRow = BlastJobSummary & {
  payload?: BlastJobSummary["payload"] & { cluster_name?: string };
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
  const { jobsQuery: blastJobsQuery } = useScopedBlastJobs({
    clusterName,
    enabled: isRunning && !isTransitioning,
    // While running, poll every 5 s as long as a submission is queued/running
    // so the cluster row reflects a fresh submit and its phase within seconds;
    // ease back to 60 s once the cluster has no active jobs. Paused entirely
    // when the cluster is stopped/transitioning.
    refetchInterval: isRunning
      ? blastJobsRefetchInterval({ activeMs: 5_000, idleMs: 60_000 })
      : false,
  });

  const tracking =
    blastJobsQuery.data != null &&
    !(blastJobsQuery.data as unknown as { degraded?: boolean }).degraded;

  const submissions: ActiveSubmission[] = (() => {
    const rows = (blastJobsQuery.data?.jobs ?? []) as ActiveSubmissionRow[];
    return rows
      .filter((r) => {
        const cluster = r.infrastructure?.cluster_name ?? r.payload?.cluster_name ?? null;
        if (cluster && cluster !== clusterName) return false;
        return isDashboardJobActive(r);
      })
      .map((r) => ({ phase: jobDisplayState(r) }));
  })();

  return { tracking, submissions };
}
