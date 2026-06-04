/**
 * usePulseSignals — single hook that gathers every live signal the
 * ClusterPulse row needs (jobs, request metrics, node summary) and
 * returns view-ready aggregates.
 *
 * Splitting this out keeps `<ClusterPulse>` itself focused on layout
 * + collapse state. The hook is intentionally read-only and gated on
 * `enabled` so a stopped/transitioning cluster does not hammer the API.
 */

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import { blastApi } from "@/api/endpoints";
import type { BlastJobSummary } from "@/api/endpoints";
import { useNodeSummary } from "@/components/ClusterDetailModal/useNodeSummary";
import {
  classifyJobState,
  isActiveJobState,
  jobClusterName,
  toJobRowView,
} from "@/components/cards/ClusterBento/jobMapping";
import type { DisplayJobState, JobRowView } from "@/components/cards/ClusterBento/jobTypes";

export const JOB_PREVIEW = 3;

const JOB_STATE_ORDER: Record<DisplayJobState, number> = {
  Queued: 0,
  Pending: 1,
  Running: 2,
  Reducing: 3,
  Failed: 4,
  Completed: 5,
  Unknown: 6,
};

export interface PulseSignals {
  /** True when /api/blast/jobs returned a `degraded` flag. */
  jobsDegraded: boolean;
  /** True while /api/blast/jobs has no cached data yet (first load).
   *  Used by `<JobsSection>` to render a skeleton roster instead of
   *  the "No jobs yet" empty state, which previously flashed for ~1s
   *  on every dashboard mount before the response landed. */
  jobsLoading: boolean;
  /** True when the node-summary fetch is degraded (used as the
   *  "metrics unavailable" signal for verdict + meta grid since the
   *  dashboard `/api/blast` request metrics are NOT a per-cluster
   *  signal — they live in the card header now). */
  metricsDegraded: boolean;

  jobRows: JobRowView[];
  jobRowsByJobId: Map<string, BlastJobSummary>;
  activeJobs: JobRowView[];
  sortedPreview: JobRowView[];
  moreJobsCount: number;

  submitsLast15m: number;
  failed15m: number;
  completedToday: number;
  /** Jobs the classifier could not bucket — surfaced so users see that
   *  the roster has rows even when active / completed counters are 0. */
  unknownCount: number;

  /** Peak user-pool CPU pct (0..1) — null when unknown. */
  cpuPct: number | null;
  /** Peak user-pool memory pct (0..1) — null when unknown. */
  memPct: number | null;
  /** max(cpu, mem) for the row-level Load stat. */
  pressureValue: number | null;

  nodeSummary: ReturnType<typeof useNodeSummary>["summary"];
  topQuery: ReturnType<typeof useNodeSummary>["topQuery"];
}

export function usePulseSignals(args: {
  subscriptionId: string;
  resourceGroup: string;
  clusterName: string;
  enabled: boolean;
}): PulseSignals {
  const { subscriptionId, resourceGroup, clusterName, enabled } = args;

  const { topQuery, summary: nodeSummary } = useNodeSummary({
    subscriptionId,
    resourceGroup,
    clusterName,
    isRunning: enabled,
  });

  const jobsQuery = useQuery({
    queryKey: ["blast-jobs", subscriptionId, resourceGroup, clusterName],
    queryFn: () =>
      blastApi.listJobs({
        subscriptionId,
        resourceGroup,
        clusterName,
      }),
    enabled,
    staleTime: 30_000,
    refetchInterval: enabled ? 60_000 : false,
    retry: 0,
  });
  // `listJobs` returns `{ jobs: [...] }` but the backend tags it with
  // `degraded: true` when the state-store table is unavailable. The
  // typed contract does not include the flag yet, so we narrow it here.
  type JobsDegraded = { degraded?: boolean };
  const jobsDegraded = (jobsQuery.data as JobsDegraded | undefined)?.degraded === true;

  // Per-cluster K8s node-summary degradation drives the "metrics
  // unavailable" UI. The dashboard `/api/blast` p95 / 5xx is a
  // process-local metric and is rendered ONCE in the card header.
  const metricsDegraded = topQuery.isError;

  const clusterJobs = useMemo<BlastJobSummary[]>(
    () => (jobsQuery.data?.jobs ?? []).filter((j) => jobClusterName(j) === clusterName),
    [jobsQuery.data, clusterName],
  );

  const jobRowsByJobId = useMemo(() => {
    const map = new Map<string, BlastJobSummary>();
    for (const j of clusterJobs) map.set(j.job_id, j);
    return map;
  }, [clusterJobs]);

  const jobRows = useMemo<JobRowView[]>(
    () => clusterJobs.map(toJobRowView),
    [clusterJobs],
  );
  const activeJobs = useMemo(
    () => jobRows.filter((j) => isActiveJobState(j.state)),
    [jobRows],
  );

  const sortedPreview = useMemo<JobRowView[]>(() => {
    const cmp = (a: JobRowView, b: JobRowView) => {
      const ta = a.createdAt ? Date.parse(a.createdAt) : 0;
      const tb = b.createdAt ? Date.parse(b.createdAt) : 0;
      if (ta !== tb) return tb - ta;
      const oa = JOB_STATE_ORDER[a.state] ?? 99;
      const ob = JOB_STATE_ORDER[b.state] ?? 99;
      return oa - ob;
    };
    return [...jobRows].sort(cmp).slice(0, JOB_PREVIEW);
  }, [jobRows]);
  const moreJobsCount = Math.max(jobRows.length - sortedPreview.length, 0);

  const { submitsLast15m, failed15m, completedToday, unknownCount } = useMemo(() => {
    const now = Date.now();
    const w15 = now - 15 * 60 * 1000;
    const w24h = now - 24 * 60 * 60 * 1000;
    let submitsLast15m = 0;
    let failed15m = 0;
    let completedToday = 0;
    let unknownCount = 0;
    for (const j of clusterJobs) {
      const ts = j.created_at ? Date.parse(j.created_at) : NaN;
      if (Number.isFinite(ts) && ts >= w15) submitsLast15m += 1;
      const upd = j.updated_at ? Date.parse(j.updated_at) : ts;
      const state = classifyJobState({
        phase: j.phase,
        status: j.status,
        error: j.error,
      });
      if (state === "Failed" && Number.isFinite(upd) && upd >= w15) {
        failed15m += 1;
      }
      if (state === "Completed" && Number.isFinite(upd) && upd >= w24h) {
        completedToday += 1;
      }
      if (state === "Unknown") unknownCount += 1;
    }
    return { submitsLast15m, failed15m, completedToday, unknownCount };
  }, [clusterJobs]);

  // Peak user-pool node — dilutes idle system nodes.
  const { cpuPct, memPct } = useMemo(() => {
    const userNodes = (topQuery.data?.nodes ?? []).filter((n) => {
      const pool = (n.pool ?? "").toLowerCase();
      return pool && pool !== "system" && !pool.startsWith("agentpool");
    });
    if (userNodes.length === 0) {
      // Fall back to the aggregate summary when we cannot distinguish
      // user vs system nodes; better than going blind.
      const fallbackCpu = nodeSummary.total > 0 ? nodeSummary.cpuPct / 100 : null;
      const fallbackMem = nodeSummary.total > 0 ? nodeSummary.memPct / 100 : null;
      return { cpuPct: fallbackCpu, memPct: fallbackMem };
    }
    let cpuMax = userNodes[0].cpu_pct ?? 0;
    let memMax = userNodes[0].memory_pct ?? 0;
    for (const n of userNodes) {
      if ((n.cpu_pct ?? 0) > cpuMax) cpuMax = n.cpu_pct ?? 0;
      if ((n.memory_pct ?? 0) > memMax) memMax = n.memory_pct ?? 0;
    }
    return { cpuPct: cpuMax / 100, memPct: memMax / 100 };
  }, [topQuery.data, nodeSummary.cpuPct, nodeSummary.memPct, nodeSummary.total]);

  const pressureValue =
    cpuPct == null && memPct == null ? null : Math.max(cpuPct ?? 0, memPct ?? 0);

  return {
    jobsDegraded,
    jobsLoading: jobsQuery.isLoading,
    metricsDegraded,
    jobRows,
    jobRowsByJobId,
    activeJobs,
    sortedPreview,
    moreJobsCount,
    submitsLast15m,
    failed15m,
    completedToday,
    unknownCount,
    cpuPct,
    memPct,
    pressureValue,
    nodeSummary,
    topQuery,
  };
}
