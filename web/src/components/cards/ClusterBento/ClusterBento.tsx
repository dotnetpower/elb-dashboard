/**
 * Bento layout for one AKS cluster — production port of the P3
 * "Mission Control Bento" mockup. Driven entirely by data the api
 * sidecar already exposes:
 *
 *  - cluster basics: `monitoringApi.aks` (already fetched by the
 *    parent `ClusterCard`).
 *  - CPU / memory aggregate: `useNodeSummary` → `k8s_top_nodes`.
 *  - Active jobs: scoped `blastApi.listJobs({ subscriptionId, resourceGroup, clusterName })`.
 *  - Submit pipeline metrics: derived from the same job list
 *    (created_at within window).
 *  - API latency / errors: `monitoringApi.requestMetrics({
 *    pathPrefix: "/api/blast" })`.
 *  - Live activity: `monitoringApi.aksEvents`.
 *
 * Cells degrade independently — when an upstream is unavailable
 * the cell renders a quiet "—" with a `<degraded reason>` hint
 * instead of disappearing or showing fabricated numbers.
 */

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  Box,
  Cpu,
  Database,
  HardDrive,
  Layers,
  Loader2,
  PlayCircle,
  Radio,
  Send,
  Shield,
  TimerReset,
} from "lucide-react";

import { blastApi, monitoringApi } from "@/api/endpoints";
import type { AksClusterSummary } from "@/api/endpoints";
import { useNodeSummary } from "@/components/ClusterDetailModal/useNodeSummary";
import {
  getAksProvisioningLabel,
  isAksProvisioning,
  isAksProvisioningFailed,
} from "@/utils/aksStatus";

import {
  BentoCell,
  EventLine,
  Eyebrow,
  HealthPill,
  JobRow,
  KpiInline,
  NumberDisplay,
  Spark,
  TrendBadge,
  fmtDuration,
} from "./atoms";
import type { ClusterHealth } from "./atoms";
import { isActiveJobState, jobClusterName, toJobRowView } from "./jobMapping";
import { groupEvents } from "./eventMapping";
import { CapacityGateCell } from "./CapacityGateCell";
import { submitTimeline, submitWindow } from "./submitMetrics";
import {
  SummaryRow,
  emptyNodeSummary,
  topologyNodesLabel,
  topologyPoolsLabel,
} from "./clusterSummaryHelpers";

const ACTIVE_JOBS_PREVIEW = 4;
const REQUEST_METRICS_WINDOW_SEC = 900; // 15 min
const EVENTS_LIMIT = 30;
const EVENT_LINES_VISIBLE = 12;
const SUBMIT_SPARK_WINDOW_MIN = 60;
/** Latency threshold (ms) above which the cluster header degrades. */
const P95_DEGRADED_MS = 2000;

interface Props {
  cluster: AksClusterSummary;
  subscriptionId: string;
  resourceGroup: string;
  isRunning: boolean;
  transition?: "starting" | "stopping";
}

export function ClusterBento({
  cluster,
  subscriptionId,
  resourceGroup,
  isRunning,
  transition,
}: Props) {
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

  const showReadinessPanel = !isRunning || transition != null;
  if (showReadinessPanel) {
    return <ClusterReadinessBento cluster={cluster} transition={transition} />;
  }

  // ---- layout -------------------------------------------------------------
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "1.2fr 1fr 1.1fr",
        gridAutoRows: "min-content",
        gap: 10,
      }}
    >
      {/* HERO — submit pipeline */}
      <BentoCell span={[2, 1]} accent="var(--accent)">
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "flex-start",
            gap: 16,
          }}
        >
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <Eyebrow>
              <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                <Send size={10} /> Submit pipeline · 15m
              </span>
            </Eyebrow>
            <NumberDisplay
              value={submitCountsUnavailable ? "—" : submits.last15m.toLocaleString()}
              unit="submits"
              size="hero"
              tone={submitCountsUnavailable ? "var(--text-faint)" : undefined}
            />
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 10,
                fontSize: 11,
                color: "var(--text-muted)",
              }}
            >
              <span style={{ fontVariantNumeric: "tabular-nums" }}>
                <span style={{ color: "var(--text-faint)" }}>1h:</span>{" "}
                {submitCountsUnavailable ? "—" : submits.last1h.toLocaleString()}
              </span>
              <span>·</span>
              <span style={{ fontVariantNumeric: "tabular-nums" }}>
                <span style={{ color: "var(--text-faint)" }}>24h:</span>{" "}
                {submitCountsUnavailable ? "—" : submits.last24h.toLocaleString()}
              </span>
              {!submitCountsUnavailable &&
                submits.delta != null &&
                submits.last24h > 0 && <TrendBadge d={submits.delta} />}
            </div>
          </div>
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              alignItems: "flex-end",
              gap: 8,
            }}
          >
            <HealthPill health={health} />
          </div>
        </div>
        <div style={{ marginTop: 10 }}>
          {!hasSubmitRequests ? (
            <EmptySubmitState
              hint={
                jobsDegraded
                  ? "No requests were returned by the available job sources."
                  : undefined
              }
            />
          ) : jobsDegraded ? (
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                fontSize: 10,
                color: "var(--text-faint)",
              }}
              title="The job-state store (Azure Table Storage) is unreachable. Submit counts, runtime stats, and the Active jobs cell all read from this store — they will recover automatically once the store comes back."
            >
              <Shield size={10} />
              job state store unavailable — counts hidden until it recovers
            </div>
          ) : submits.last24h === 0 ? (
            <EmptySubmitState />
          ) : submitSpark.length > 0 ? (
            <>
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "baseline",
                  fontSize: 10,
                  color: "var(--text-faint)",
                  marginBottom: 2,
                }}
              >
                <span>Submits per minute · last {SUBMIT_SPARK_WINDOW_MIN}m</span>
                <span style={{ fontVariantNumeric: "tabular-nums" }}>
                  peak: {submitSparkPeak.toLocaleString()}/min
                </span>
              </div>
              <Spark
                data={submitSpark}
                color="var(--accent)"
                width={520}
                height={44}
                ariaLabel={`Submits per minute, last ${SUBMIT_SPARK_WINDOW_MIN} minutes`}
              />
            </>
          ) : (
            <DegradedHint
              reason={
                metricsDegraded
                  ? "metrics not yet collected"
                  : `no submits in the last ${SUBMIT_SPARK_WINDOW_MIN}m`
              }
            />
          )}
        </div>
      </BentoCell>

      {/* LIVE ACTIVITY rail — spans the full height of the left column.
          When the Active jobs cell collapses (job-state degraded), the
          rail trims by one row so the right column doesn't grow taller
          than the left. */}
      <BentoCell span={[1, submitCountsUnavailable ? 3 : 4]}>
        <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 8 }}>
          <Radio size={11} color="var(--accent)" />
          <Eyebrow>Live activity</Eyebrow>
          {events.length > 0 && (
            <span
              style={{
                marginLeft: "auto",
                fontSize: 9,
                color: "var(--text-faint)",
                fontVariantNumeric: "tabular-nums",
              }}
              title="Number of raw k8s events folded into the visible rows"
            >
              {events.length} events
            </span>
          )}
        </div>
        <div
          style={{ display: "flex", flexDirection: "column", gap: 1, overflow: "auto" }}
        >
          {eventLines.length === 0 ? (
            <DegradedHint
              reason={
                eventsDegraded
                  ? "events not available (cluster stopped or RBAC denied)"
                  : "no recent events"
              }
            />
          ) : (
            <>
              {eventLines.map((e) => (
                <EventLine
                  key={e.key}
                  kind={e.kind}
                  message={e.message}
                  time={e.time}
                  compact
                />
              ))}
              {events.length > eventLinesShownEvents && (
                <div
                  style={{
                    marginTop: 4,
                    padding: "4px 10px",
                    fontSize: 10,
                    color: "var(--text-faint)",
                    fontStyle: "italic",
                  }}
                >
                  +{events.length - eventLinesShownEvents} older events not shown
                </div>
              )}
            </>
          )}
        </div>
      </BentoCell>

      {/* PULSE strip — p95 / errors / CPU / Memory */}
      <BentoCell span={[2, 1]}>
        <div style={{ display: "flex", alignItems: "center", gap: 16, padding: "2px 0" }}>
          <KpiInline
            icon={<TimerReset size={11} />}
            label="API p95"
            tone={p95Tone}
            value={p95 == null ? "—" : `${Math.round(p95)}`}
            hint={p95 == null ? undefined : `ms · SLA ${P95_DEGRADED_MS}`}
            bar={p95 == null ? undefined : Math.min(p95 / P95_DEGRADED_MS, 1)}
            title={
              apiRpmPeak > 0
                ? `Last 15m · ${apiRpmPeak} peak rpm · SLA ${P95_DEGRADED_MS} ms`
                : `SLA ${P95_DEGRADED_MS} ms`
            }
          />
          <span className="bento-pulse-divider" aria-hidden="true" />
          <KpiInline
            icon={<Shield size={11} />}
            label="Errors 15m"
            tone={errTone}
            value={metrics ? apiErrors.toString() : "—"}
            hint={
              metrics && metrics.total > 0
                ? `${(metrics.error_rate * 100).toFixed(1)}%`
                : undefined
            }
            title={
              metrics
                ? `${metrics.total ?? 0} total requests · ${apiErrors} errored`
                : undefined
            }
          />
          <span className="bento-pulse-divider" aria-hidden="true" />
          <KpiInline
            icon={<Cpu size={11} />}
            label="CPU peak"
            tone={cpuTone}
            value={cpuPct == null ? "—" : `${(cpuPct * 100).toFixed(0)}%`}
            hint={
              userNodePeaks.cpu != null && nodeSummary.total > 0
                ? `(avg ${nodeSummary.cpuPct.toFixed(0)}%)`
                : undefined
            }
            bar={cpuPct ?? undefined}
            title={
              userNodePeaks.cpuNode
                ? `Hottest user-pool node: ${userNodePeaks.cpuNode}`
                : undefined
            }
          />
          <span className="bento-pulse-divider" aria-hidden="true" />
          <KpiInline
            icon={<HardDrive size={11} />}
            label="Mem peak"
            tone={memTone}
            value={memPct == null ? "—" : `${(memPct * 100).toFixed(0)}%`}
            hint={
              userNodePeaks.mem != null && nodeSummary.total > 0
                ? `(avg ${nodeSummary.memPct.toFixed(0)}%)`
                : undefined
            }
            bar={memPct ?? undefined}
            title={
              userNodePeaks.memNode
                ? `Hottest user-pool node: ${userNodePeaks.memNode}`
                : undefined
            }
          />
        </div>
        {topQuery.isError && <DegradedHint reason="node metrics unavailable" />}
      </BentoCell>

      {/* ACTIVE JOBS preview — collapses to a single row when the job
          state store is unreachable so the bento doesn't leave a giant
          empty grey box. The HERO cell already surfaces the degraded
          state, so this cell stays quiet. */}
      <BentoCell span={[2, submitCountsUnavailable ? 1 : 2]}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            marginBottom: submitCountsUnavailable ? 0 : 10,
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <PlayCircle size={11} color="var(--accent)" />
            <Eyebrow>
              {submitCountsUnavailable
                ? "Active jobs"
                : `Active jobs · ${activeJobs.length.toString()}`}
            </Eyebrow>
            {submitCountsUnavailable && (
              <span
                style={{
                  fontSize: 10,
                  color: "var(--text-faint)",
                  fontStyle: "italic",
                  marginLeft: 6,
                }}
              >
                — hidden while job store is unreachable
              </span>
            )}
          </div>
          {failed15m > 0 && (
            <span
              style={{
                fontSize: 10,
                color: "var(--danger)",
                fontWeight: 600,
                letterSpacing: "0.04em",
                textTransform: "uppercase",
              }}
            >
              {failed15m} FAILED · 15m
            </span>
          )}
        </div>
        {submitCountsUnavailable ? null : (
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {activeJobs.length === 0 ? (
              <DegradedHint reason="no active jobs" />
            ) : (
              <>
                {activeJobs.slice(0, ACTIVE_JOBS_PREVIEW).map((j) => (
                  <JobRow key={j.jobId} j={j} dense />
                ))}
                {activeJobs.length > ACTIVE_JOBS_PREVIEW && (
                  <div
                    style={{
                      marginTop: 4,
                      padding: "6px 10px",
                      background: "transparent",
                      border: "1px dashed var(--border-weak)",
                      borderRadius: 6,
                      color: "var(--text-muted)",
                      fontSize: 11,
                    }}
                  >
                    +{activeJobs.length - ACTIVE_JOBS_PREVIEW} more active jobs
                  </div>
                )}
              </>
            )}
          </div>
        )}
      </BentoCell>

      {/* CLUSTER topology summary — useful when nothing is running */}
      <BentoCell span={[1, 1]}>
        <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 8 }}>
          <Layers size={11} color="var(--accent)" />
          <Eyebrow>Topology</Eyebrow>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 6, fontSize: 11 }}>
          <SummaryRow
            icon={<Box size={11} />}
            label="Nodes"
            value={topologyNodesLabel(cluster, nodeSummary)}
            hint={
              nodeSummary.notReady > 0 ? `${nodeSummary.notReady} not-ready` : undefined
            }
          />
          <SummaryRow
            icon={<Cpu size={11} />}
            label="SKU"
            value={cluster.node_sku ?? "—"}
          />
          <SummaryRow
            icon={<Database size={11} />}
            label="Pools"
            value={topologyPoolsLabel(cluster, nodeSummary)}
          />
          <SummaryRow
            icon={<Activity size={11} />}
            label="K8s"
            value={cluster.k8s_version ?? "—"}
          />
        </div>
      </BentoCell>

      {/* ACTIVITY summary cell — small placeholder for parity with mockup */}
      <BentoCell span={[1, 1]}>
        <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 8 }}>
          <Activity size={11} color="var(--accent)" />
          <Eyebrow>Recent runtime · 24h</Eyebrow>
        </div>
        {submitCountsUnavailable ? (
          // Don't repeat the "job state unavailable" hint here — the
          // HERO cell already surfaces it. Render a small explanatory
          // line so this cell stays vertically balanced with the
          // Topology cell next to it.
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 4,
              fontSize: 11,
              color: "var(--text-faint)",
            }}
            title="Recent-runtime statistics are derived from finished jobs in the job-state store. They will reappear once the store recovers."
          >
            <div style={{ fontSize: 18, lineHeight: 1, color: "var(--text-faint)" }}>
              —
            </div>
            <span style={{ fontStyle: "italic" }}>runtime stats hidden</span>
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <NumberDisplay
              value={(submits.last24h - submits.last24hActive).toLocaleString()}
              unit="finished"
              size="lg"
            />
            <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
              {submits.last24hActive.toLocaleString()} still running · avg{" "}
              {fmtDuration(submits.avgRuntimeSec)}
            </div>
          </div>
        )}
      </BentoCell>

      {/* CAPACITY GATE — slot count, watermarks, decision preview */}
      <CapacityGateCell
        subscriptionId={subscriptionId}
        resourceGroup={resourceGroup}
        clusterName={cluster.name}
        isRunning={isRunning}
      />
    </div>
  );
}

function DegradedHint({ reason }: { reason: string }) {
  return (
    <div
      style={{
        fontSize: 11,
        color: "var(--text-faint)",
        fontStyle: "italic",
        padding: "8px 4px",
      }}
    >
      {reason}
    </div>
  );
}

function ClusterReadinessBento({
  cluster,
  transition,
}: {
  cluster: AksClusterSummary;
  transition?: "starting" | "stopping";
}) {
  const provisioningLabel = getAksProvisioningLabel(cluster);
  const isStarting =
    transition === "starting" ||
    provisioningLabel === "Starting" ||
    cluster.power_state === "Starting";
  const isStopping = transition === "stopping";
  const isProvisioning = isAksProvisioning(cluster);
  const title = isStarting
    ? "Cluster is starting"
    : isStopping
      ? "Cluster is stopping"
      : cluster.power_state === "Stopped"
        ? "Cluster is stopped"
        : isProvisioning
          ? "Cluster is provisioning"
          : "Cluster is not workload-ready";
  const body = isStarting
    ? "AKS is coming online. Submit metrics, node activity, and warm-cache controls appear after the workload nodes report Running."
    : isStopping
      ? "AKS is shutting down. Live workload metrics are paused until the next start completes."
      : cluster.power_state === "Stopped"
        ? "Start the cluster to enable submit monitoring, node metrics, and automatic warmup."
        : "The control plane can see this cluster, but workload checks are not ready yet.";
  const nextStep = isStarting
    ? "Auto warm will be reconciled by Celery after the cluster becomes ready."
    : isStopping
      ? "Queued Celery work remains tracked while the browser can be refreshed safely."
      : cluster.power_state === "Stopped"
        ? "Use Start on the cluster header when you are ready to run BLAST jobs."
        : "Keep this view open or refresh later; the dashboard will update automatically.";

  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "minmax(0, 1.4fr) minmax(260px, 0.8fr)",
        gap: 10,
      }}
    >
      <BentoCell span={[1, 1]} accent={isStarting ? "var(--accent)" : "var(--warning)"}>
        <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
          <div
            style={{
              width: 34,
              height: 34,
              borderRadius: 8,
              display: "grid",
              placeItems: "center",
              background: isStarting
                ? "rgba(122, 167, 255, 0.12)"
                : "rgba(240, 198, 116, 0.10)",
              color: isStarting ? "var(--accent)" : "var(--warning)",
              flexShrink: 0,
            }}
          >
            {isStarting || isStopping || isAksProvisioning(cluster) ? (
              <Loader2 size={17} className="spin" />
            ) : (
              <PlayCircle size={17} />
            )}
          </div>
          <div style={{ minWidth: 0, display: "flex", flexDirection: "column", gap: 8 }}>
            <div
              style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}
            >
              <div
                style={{ fontSize: 16, fontWeight: 650, color: "var(--text-primary)" }}
              >
                {title}
              </div>
              <ReadinessPill
                label={
                  isStarting
                    ? "Starting"
                    : isStopping
                      ? "Stopping"
                      : (provisioningLabel ?? cluster.power_state ?? "Waiting")
                }
                tone={
                  isStarting
                    ? "var(--accent)"
                    : isStopping
                      ? "var(--warning)"
                      : "var(--text-muted)"
                }
                spinning={isStarting || isStopping || isProvisioning}
              />
            </div>
            <div
              style={{
                fontSize: 12,
                lineHeight: 1.55,
                color: "var(--text-muted)",
                maxWidth: 680,
              }}
            >
              {body}
            </div>
            <div
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                width: "fit-content",
                padding: "6px 9px",
                border: "1px solid var(--border-weak)",
                borderRadius: 7,
                color: "var(--text-muted)",
                background: "rgba(255,255,255,0.025)",
                fontSize: 11,
              }}
            >
              <Activity size={11} color="var(--accent)" />
              {nextStep}
            </div>
          </div>
        </div>
      </BentoCell>

      <BentoCell span={[1, 1]}>
        <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 8 }}>
          <Layers size={11} color="var(--accent)" />
          <Eyebrow>Cluster summary</Eyebrow>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 7, fontSize: 11 }}>
          <SummaryRow
            icon={<Box size={11} />}
            label="Nodes"
            value={cluster.node_count?.toString() ?? "—"}
          />
          <SummaryRow
            icon={<Cpu size={11} />}
            label="SKU"
            value={cluster.node_sku ?? "—"}
          />
          <SummaryRow
            icon={<Database size={11} />}
            label="Pools"
            value={
              cluster.agent_pools?.length
                ? topologyPoolsLabel(cluster, emptyNodeSummary())
                : "—"
            }
          />
          <SummaryRow
            icon={<Activity size={11} />}
            label="K8s"
            value={cluster.k8s_version ?? "—"}
          />
        </div>
      </BentoCell>
    </div>
  );
}

function ReadinessPill({
  label,
  tone,
  spinning,
}: {
  label: string;
  tone: string;
  spinning: boolean;
}) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        padding: "3px 8px",
        borderRadius: 999,
        border: "1px solid var(--border-weak)",
        color: tone,
        background: "rgba(255,255,255,0.03)",
        fontSize: 10,
        fontWeight: 650,
        letterSpacing: "0.03em",
        textTransform: "uppercase",
      }}
    >
      {spinning && <Loader2 size={10} className="spin" />}
      {label}
    </span>
  );
}

function EmptySubmitState({ hint }: { hint?: string }) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "flex-start",
        gap: 12,
        padding: "10px 12px",
        marginTop: 4,
        background: "rgba(110,159,255,0.04)",
        border: "1px dashed rgba(110,159,255,0.20)",
        borderRadius: 8,
      }}
    >
      <div style={{ fontSize: 11, color: "var(--text-muted)", lineHeight: 1.5 }}>
        No submit requests yet.{" "}
        <span style={{ color: "var(--text-faint)" }}>
          Start a BLAST run to populate this card.
        </span>
        {hint && (
          <div style={{ marginTop: 2, color: "var(--text-faint)", fontSize: 10 }}>
            {hint}
          </div>
        )}
      </div>
    </div>
  );
}

