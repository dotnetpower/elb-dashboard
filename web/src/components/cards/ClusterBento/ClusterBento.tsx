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

import {
  Activity,
  Box,
  Cpu,
  Database,
  HardDrive,
  Layers,
  PlayCircle,
  Radio,
  Send,
  Shield,
  TimerReset,
} from "lucide-react";

import type { AksClusterSummary } from "@/api/endpoints";

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
import { CapacityGateCell } from "./CapacityGateCell";
import { ClusterReadinessBento } from "./ClusterReadinessBento";
import {
  SUBMIT_SPARK_WINDOW_MIN,
  useClusterBentoModel,
} from "./useClusterBentoModel";
import {
  SummaryRow,
  topologyNodesLabel,
  topologyPoolsLabel,
} from "./clusterSummaryHelpers";

const ACTIVE_JOBS_PREVIEW = 4;
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
  const {
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
  } = useClusterBentoModel({ cluster, subscriptionId, resourceGroup, isRunning });

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

