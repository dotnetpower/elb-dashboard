import type { BlastJobSummary } from "@/api/endpoints";
import { blastApi } from "@/api/endpoints";
import { blastJobsRefetchInterval, useScopedBlastJobs } from "@/hooks/useScopedBlastJobs";
import { useQuery } from "@tanstack/react-query";
import { useMatch } from "react-router-dom";

/**
 * Polls the job source for the topbar chip. On a BLAST job detail route,
 * returns that active job so the header mirrors the page the researcher is
 * looking at. Everywhere else, returns the single most recent job by
 * `updated_at`, falling back to `created_at`.
 *
 * Single-responsibility: data acquisition + "which job counts as the
 * latest" — no rendering, no formatting, no styling decisions.
 */
export interface UseLatestBlastJobResult {
  /** The active detail job or latest job, or `null` when none is available. */
  job: BlastJobSummary | null;
  /** True until the very first response arrives. */
  isLoading: boolean;
  /** True when the request errored. The chip treats this as "hide". */
  isError: boolean;
}

export function useLatestBlastJob(): UseLatestBlastJobResult {
  const exactJobMatch = useMatch("/blast/jobs/:jobId");
  const nestedJobMatch = useMatch("/blast/jobs/:jobId/*");
  const activeJobId = exactJobMatch?.params.jobId ?? nestedJobMatch?.params.jobId ?? "";

  const { jobsQuery } = useScopedBlastJobs({
    // The topbar chip shows the single most recent job. List across every
    // cluster so a job on a peer cluster isn't masked by an auto-pinned
    // (often Stopped, often stale) cluster.
    autoSelectCluster: false,
    // Poll fast while a job is queued/running so the chip tracks live status;
    // ease off to 15 s once every job is terminal.
    refetchInterval: blastJobsRefetchInterval({ activeMs: 5_000, idleMs: 15_000 }),
  });

  const activeJobQuery = useQuery({
    queryKey: ["blast-job", activeJobId],
    queryFn: () => blastApi.getJob(activeJobId, { includeDatabaseMetadata: false }),
    enabled: Boolean(activeJobId),
    refetchOnWindowFocus: true,
  });

  const jobs = jobsQuery.data?.jobs ?? [];
  const job = selectTopbarBlastJob({
    activeJobId,
    activeJob: activeJobQuery.data,
    jobs,
  });

  if (activeJobId) {
    return {
      job,
      isLoading: !job && activeJobQuery.isLoading,
      isError: !job && activeJobQuery.isError,
    };
  }

  return { job, isLoading: jobsQuery.isLoading, isError: jobsQuery.isError };
}

export function selectTopbarBlastJob({
  activeJobId,
  activeJob,
  jobs,
}: {
  activeJobId?: string;
  activeJob?: BlastJobSummary;
  jobs: BlastJobSummary[];
}): BlastJobSummary | null {
  if (activeJobId) {
    return activeJob ?? jobs.find((job) => job.job_id === activeJobId) ?? null;
  }
  return jobs.length === 0 ? null : pickLatest(jobs);
}

export function pickLatest(jobs: BlastJobSummary[]): BlastJobSummary {
  return [...jobs].sort((a, b) => {
    const ta = Date.parse(a.updated_at || a.created_at || "") || 0;
    const tb = Date.parse(b.updated_at || b.created_at || "") || 0;
    return tb - ta;
  })[0];
}
