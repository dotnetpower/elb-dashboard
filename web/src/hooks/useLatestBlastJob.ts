import { useQuery } from "@tanstack/react-query";

import { blastApi, type BlastJobSummary } from "@/api/endpoints";

/**
 * Polls `/api/blast/jobs` and returns the single most recent job (by
 * `updated_at`, falling back to `created_at`).
 *
 * Single-responsibility: data acquisition + "which job counts as the
 * latest" — no rendering, no formatting, no styling decisions.
 */
export interface UseLatestBlastJobResult {
  /** The latest job, or `null` when the tenant has none yet. */
  job: BlastJobSummary | null;
  /** True until the very first response arrives. */
  isLoading: boolean;
  /** True when the request errored. The chip treats this as "hide". */
  isError: boolean;
}

export function useLatestBlastJob(): UseLatestBlastJobResult {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["latest-blast-job"],
    queryFn: () => blastApi.listJobs(),
    // Researcher leaves the dashboard open on a second monitor — keep
    // it warm but cheap. 15 s matches the existing dashboard cadence.
    refetchInterval: 15_000,
    staleTime: 10_000,
  });

  const jobs = data?.jobs ?? [];
  const job = jobs.length === 0 ? null : pickLatest(jobs);

  return { job, isLoading, isError };
}

function pickLatest(jobs: BlastJobSummary[]): BlastJobSummary {
  return [...jobs].sort((a, b) => {
    const ta = Date.parse(a.updated_at || a.created_at || "") || 0;
    const tb = Date.parse(b.updated_at || b.created_at || "") || 0;
    return tb - ta;
  })[0];
}
