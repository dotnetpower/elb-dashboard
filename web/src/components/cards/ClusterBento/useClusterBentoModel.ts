/**
 * Data-orchestration hook for {@link ClusterBento}.
 *
 * Owns every upstream query (node top metrics, scoped BLAST jobs, `/api/blast`
 * request metrics, AKS events) and the derived view models the bento grid
 * renders (submit window/timeline, active-job rows, API latency/error tones,
 * peak user-pool CPU/mem, and the overall cluster health verdict). Keeping
 * this out of `ClusterBento.tsx` lets that file own presentation only.
 *
 * Every cell degrades independently: when an upstream is unavailable the hook
 * still returns a usable model (nulls / empty arrays / `degraded` flags) so the
 * grid renders quiet "—" placeholders instead of disappearing or fabricating
 * numbers.
 */

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import { blastApi, monitoringApi } from "@/api/endpoints";
import type { AksClusterSummary } from "@/api/endpoints";
import { useNodeSummary } from "@/components/ClusterDetailModal/useNodeSummary";
import { isAksProvisioning, isAksProvisioningFailed } from "@/utils/aksStatus";

import type { ClusterHealth } from "./atoms";
import { isActiveJobState, jobClusterName, toJobRowView } from "./jobMapping";
import { groupEvents } from "./eventMapping";
import { submitTimeline, submitWindow } from "./submitMetrics";

const REQUEST_METRICS_WINDOW_SEC = 900; // 15 min
const EVENTS_LIMIT = 30;
const EVENT_LINES_VISIBLE = 12;
/** Submit sparkline window (minutes). Shared with the label in the bento grid. */
export const SUBMIT_SPARK_WINDOW_MIN = 60;

interface ClusterBentoModelArgs {
  cluster: AksClusterSummary;
  subscriptionId: string;
  resourceGroup: string;
  isRunning: boolean;
}

export function useClusterBentoModel({
  cluster,
  subscriptionId,
  resourceGroup,
  isRunning,
}: ClusterBentoModelArgs) {
  // ---- data sources -------------------------------------------------------
  const { topQuery, summary: nodeSummary } = useNodeSummary({
    subscriptionId,
    resourceGroup,
    clusterName: cluster.name,
    isRunning,
  });

  const jobsQuery = useQuery({
    queryKey: ["blast-jobs", subscriptionId, resourceGroup, cluster.name],
    queryFn: () =>
      blastApi.listJobs({
        subscriptionId,
        resourceGroup,
        clusterName: cluster.name,
      }),
    enabled: isRunning,
    staleTime: 30_000,
    refetchInterval: isRunning ? 60_000 : false,
    retry: 0,
  });
  const jobsDegraded =
    (jobsQuery.data as unknown as { degraded?: boolean } | undefined)?.degraded === true;

  const metricsQuery = useQuery({
    queryKey: ["request-metrics-blast", REQUEST_METRICS_WINDOW_SEC],
    queryFn: () =>
      monitoringApi.requestMetrics({
        windowSeconds: REQUEST_METRICS_WINDOW_SEC,
        pathPrefix: "/api/blast",
        rpmBuckets: 60,
      }),
    enabled: isRunning,
    staleTime: 25_000,
    refetchInterval: isRunning ? 30_000 : false,
    retry: 0,
  });

  const eventsQuery = useQuery({
    queryKey: ["aks-events", subscriptionId, resourceGroup, cluster.name, EVENTS_LIMIT],
    queryFn: () =>
      monitoringApi.aksEvents(subscriptionId, resourceGroup, cluster.name, {
        limit: EVENTS_LIMIT,
      }),
    enabled: isRunning,
    staleTime: 20_000,
    refetchInterval: isRunning ? 45_000 : false,
    retry: 0,
  });

  // ---- derived view models -----------------------------------------------
  const allJobs = useMemo(() => jobsQuery.data?.jobs ?? [], [jobsQuery.data]);

  const clusterJobs = useMemo(
    () => allJobs.filter((j) => jobClusterName(j) === cluster.name),
    [allJobs, cluster.name],
  );
  const hasSubmitRequests = clusterJobs.length > 0;
  const submitCountsUnavailable = jobsDegraded && hasSubmitRequests;

  const jobRows = useMemo(() => clusterJobs.map(toJobRowView), [clusterJobs]);
  const activeJobs = useMemo(
    () => jobRows.filter((j) => isActiveJobState(j.state)),
    [jobRows],
  );
  const failed15m = useMemo(() => {
    const cutoff = Date.now() - 15 * 60 * 1000;
    return clusterJobs.filter(
      (j) =>
        (j.status === "failed" || j.phase === "Failed") &&
        j.updated_at &&
        Date.parse(j.updated_at) >= cutoff,
    ).length;
  }, [clusterJobs]);

  const submits = useMemo(() => submitWindow(clusterJobs), [clusterJobs]);
  const submitSpark = useMemo(
    () => submitTimeline(clusterJobs, SUBMIT_SPARK_WINDOW_MIN),
    [clusterJobs],
  );
  const submitSparkPeak = useMemo(
    () => (submitSpark.length === 0 ? 0 : Math.max(...submitSpark)),
    [submitSpark],
  );

  const metrics = metricsQuery.data;
  const metricsDegraded =
    (metrics as { degraded?: boolean } | undefined)?.degraded === true;
  const p95 = metrics?.p95_ms ?? null;
  const apiErrors = metrics?.errors ?? 0;
  const apiRpmPeak = useMemo<number>(
    () => (metrics?.rpm ?? []).reduce((m, b) => (b.count > m ? b.count : m), 0),
    [metrics],
  );

  const events = useMemo(() => eventsQuery.data?.events ?? [], [eventsQuery.data]);
  const eventsDegraded =
    (eventsQuery.data as unknown as { degraded?: boolean } | undefined)?.degraded ===
    true;
  const eventLines = useMemo(() => groupEvents(events, EVENT_LINES_VISIBLE), [events]);
  /** Events folded into the visible 12 (sum of group counts). */
  const eventLinesShownEvents = useMemo(
    () => eventLines.reduce((sum, e) => sum + e.count, 0),
    [eventLines],
  );

  // Peak (most-loaded) user-pool node — far more useful than the
  // cluster-wide average, which dilutes hot user nodes against idle
  // system nodes.
  const nodes = useMemo(() => topQuery.data?.nodes ?? [], [topQuery.data]);
  const userNodePeaks = useMemo(() => {
    const userNodes = nodes.filter((n) => {
      const pool = (n.pool ?? "").toLowerCase();
      return pool && pool !== "system" && !pool.startsWith("agentpool");
    });
    if (userNodes.length === 0) return { cpu: null, mem: null, cpuNode: "", memNode: "" };
    let cpuMaxNode = userNodes[0];
    let memMaxNode = userNodes[0];
    for (const n of userNodes) {
      if ((n.cpu_pct ?? 0) > (cpuMaxNode.cpu_pct ?? 0)) cpuMaxNode = n;
      if ((n.memory_pct ?? 0) > (memMaxNode.memory_pct ?? 0)) memMaxNode = n;
    }
    return {
      cpu: (cpuMaxNode.cpu_pct ?? 0) / 100,
      mem: (memMaxNode.memory_pct ?? 0) / 100,
      cpuNode: cpuMaxNode.name,
      memNode: memMaxNode.name,
    };
  }, [nodes]);

  const cpuPct =
    userNodePeaks.cpu ?? (nodeSummary.total > 0 ? nodeSummary.cpuPct / 100 : null);
  const memPct =
    userNodePeaks.mem ?? (nodeSummary.total > 0 ? nodeSummary.memPct / 100 : null);
  const cpuTone =
    cpuPct == null
      ? "var(--text-muted)"
      : cpuPct >= 0.85
        ? "var(--danger)"
        : cpuPct >= 0.7
          ? "var(--warning)"
          : "var(--teal)";
  const memTone =
    memPct == null
      ? "var(--text-muted)"
      : memPct >= 0.85
        ? "var(--danger)"
        : memPct >= 0.7
          ? "var(--warning)"
          : "var(--teal)";
  const p95Tone =
    p95 == null
      ? "var(--text-muted)"
      : p95 > 2000
        ? "var(--danger)"
        : p95 > 1000
          ? "var(--warning)"
          : "var(--text-primary)";
  const errTone =
    apiErrors > 5 ? "var(--danger)" : apiErrors > 0 ? "var(--warning)" : "var(--success)";

  const health: ClusterHealth = useMemo(() => {
    if (isAksProvisioning(cluster)) return "provisioning";
    if (isAksProvisioningFailed(cluster)) return "degraded";
    if (!isRunning) return "down";
    if (cluster.power_state && cluster.power_state !== "Running") return "down";
    if (nodeSummary.notReady > 0 || nodeSummary.pressure.length > 0) return "degraded";
    if (cpuPct != null && cpuPct >= 0.95) return "degraded";
    if (memPct != null && memPct >= 0.95) return "degraded";
    if (jobsDegraded && metricsDegraded && eventsDegraded && nodeSummary.total === 0) {
      return "unknown";
    }
    return "healthy";
  }, [
    isRunning,
    cluster,
    cpuPct,
    memPct,
    jobsDegraded,
    metricsDegraded,
    eventsDegraded,
    nodeSummary.total,
    nodeSummary.notReady,
    nodeSummary.pressure.length,
  ]);

  return {
    topQuery,
    nodeSummary,
    jobsDegraded,
    hasSubmitRequests,
    submitCountsUnavailable,
    activeJobs,
    failed15m,
    submits,
    submitSpark,
    submitSparkPeak,
    metrics,
    metricsDegraded,
    p95,
    apiErrors,
    apiRpmPeak,
    events,
    eventsDegraded,
    eventLines,
    eventLinesShownEvents,
    userNodePeaks,
    cpuPct,
    memPct,
    cpuTone,
    memTone,
    p95Tone,
    errTone,
    health,
  };
}
