/**
 * ClusterPulse — single-line "pulse" representation of an AKS cluster
 * on the dashboard. Shell only: data shaping lives in `usePulseSignals`
 * and `useClusterHealth`; UI lives in the sibling presentation modules.
 *
 *   [● tone] cluster-name · status-line ... [Submits 15m] [Active] [Pressure] ▾
 *
 * Expanding the row reveals a meta grid → jobs roster → caller-supplied
 * extras (sharding chips, start-estimate panel) → action buttons.
 */

import { useId, useState } from "react";

import type { AksClusterSummary } from "@/api/endpoints";
import {
  isAksProvisioning,
  isAksWorkloadReady,
} from "@/utils/aksStatus";

import { JobsSection } from "./JobsSection";
import { PulseActions } from "./PulseActions";
import { PulseMetaGrid } from "./PulseMetaGrid";
import { PulseRowSummary } from "./PulseRowSummary";
import { useClusterHealth } from "./useClusterHealth";
import { usePulseSignals } from "./usePulseSignals";

const COLLAPSED_KEY = "elb-cluster-pulse-collapsed-";

export interface DbCounts {
  /** Databases visible to the cluster (chip count). */
  ready: number;
  /** Databases that cannot warm up on the current cluster topology. */
  unavailable: number;
}

interface Props {
  cluster: AksClusterSummary;
  subscriptionId: string;
  resourceGroup: string;
  trans?: "starting" | "stopping";
  actionLoading: string | null;
  onStartStop: (name: string, action: "start" | "stop") => void;
  onDelete: (name: string) => void;
  /** Supplied by the parent so the row's DB cell mirrors the chip strip. */
  dbCounts?: DbCounts;
  /** Children rendered between Jobs and Actions (sharding chip strip,
   *  StartEstimatePanel, …). The parent owns visibility. */
  expansionExtras?: React.ReactNode;
  /** Invoked by the "Open cluster detail" button and the "+N more jobs"
   *  affordance. The parent owns the modal. */
  onOpenDetail: () => void;
}

export function ClusterPulse({
  cluster: c,
  subscriptionId,
  resourceGroup,
  trans,
  actionLoading,
  onStartStop,
  onDelete,
  dbCounts,
  expansionExtras,
  onOpenDetail,
}: Props) {
  const isStopped = c.power_state === "Stopped";
  const isRunning = isAksWorkloadReady(c);
  const isTransitioning = trans != null;
  const showOperationalDetails = isRunning && !isTransitioning;
  const provisioningBusy = isAksProvisioning(c);

  // Default-open rules: stopped/transitioning/provisioning are open so
  // the operator sees the Start button + readiness panel immediately.
  const defaultOpen =
    isStopped || isTransitioning || provisioningBusy || !isRunning;

  const [open, setOpen] = useState(() => {
    try {
      const v = localStorage.getItem(COLLAPSED_KEY + c.name);
      return v != null ? v === "0" : defaultOpen;
    } catch {
      return defaultOpen;
    }
  });
  const toggleOpen = () => {
    setOpen((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(COLLAPSED_KEY + c.name, next ? "0" : "1");
      } catch {
        /* ignore */
      }
      return next;
    });
  };

  const signals = usePulseSignals({
    subscriptionId,
    resourceGroup,
    clusterName: c.name,
    enabled: showOperationalDetails,
  });

  const panelId = useId();

  const health = useClusterHealth({
    cluster: c,
    isRunning,
    isTransitioning,
    provisioningBusy,
    trans,
    cpuPct: signals.cpuPct,
    memPct: signals.memPct,
    apiP95: signals.apiP95,
    apiErrors: signals.apiErrors,
    jobsDegraded: signals.jobsDegraded,
    metricsDegraded: signals.metricsDegraded,
    nodeNotReady: signals.nodeSummary.notReady,
    nodePressureCount: signals.nodeSummary.pressure.length,
    nodeTotal: signals.nodeSummary.total,
  });

  return (
    <div
      className="glass-card cluster-pulse-card"
      style={{ padding: 0, borderRadius: 12, overflow: "hidden" }}
    >
      <PulseRowSummary
        clusterName={c.name}
        tone={health.tone}
        statusTone={health.statusTone}
        statusLine={health.statusLine}
        open={open}
        onToggle={toggleOpen}
        panelId={panelId}
        submits15m={
          !showOperationalDetails || signals.jobsDegraded
            ? "—"
            : signals.submitsLast15m.toLocaleString()
        }
        activeCount={
          !showOperationalDetails || signals.jobsDegraded
            ? "—"
            : signals.activeJobs.length.toString()
        }
        activeTone={
          signals.activeJobs.length > 5 ? "var(--warning)" : undefined
        }
        pressureLabel={
          !showOperationalDetails || signals.pressureValue == null
            ? "—"
            : `${Math.round(signals.pressureValue * 100)}%`
        }
        pressureTone={
          signals.pressureValue == null
            ? undefined
            : signals.pressureValue >= 0.85
              ? "var(--danger)"
              : signals.pressureValue >= 0.7
                ? "var(--warning)"
                : undefined
        }
      />

      {open && (
        <div
          id={panelId}
          style={{
            borderTop: "1px solid var(--border-weak)",
            background: "var(--pulse-body-bg)",
          }}
          onClick={(e) => e.stopPropagation()}
        >
          {/* Actions live at the top of the expanded panel so Start /
              Stop / Open detail / Delete are reachable without
              scrolling past the Jobs roster. */}
          <PulseActions
            cluster={c}
            trans={trans}
            actionLoading={actionLoading}
            onStartStop={onStartStop}
            onDelete={onDelete}
            onOpenDetail={onOpenDetail}
          />

          <PulseMetaGrid
            region={c.region ?? "—"}
            k8sVersion={c.k8s_version ?? "—"}
            nodeCountLabel={
              signals.nodeSummary.total > 0
                ? signals.nodeSummary.total.toString()
                : c.node_count != null
                  ? c.node_count.toString()
                  : "—"
            }
            dbCountsLabel={
              dbCounts == null
                ? "—"
                : dbCounts.unavailable > 0
                  ? `${dbCounts.ready} visible · ${dbCounts.unavailable} infeasible`
                  : `${dbCounts.ready} visible`
            }
            cpuPct={signals.cpuPct}
            memPct={signals.memPct}
            apiP95Ms={signals.apiP95}
            apiErrors={signals.apiErrors}
            metricsDegraded={signals.metricsDegraded}
          />

          {showOperationalDetails && (
            <JobsSection
              jobs={signals.sortedPreview}
              moreCount={signals.moreJobsCount}
              activeCount={signals.activeJobs.length}
              completedToday={signals.completedToday}
              failed15m={signals.failed15m}
              unknownCount={signals.unknownCount}
              jobsDegraded={signals.jobsDegraded}
              jobsLoading={signals.jobsLoading}
              jobIndex={signals.jobRowsByJobId}
              clusterName={c.name}
            />
          )}

          {expansionExtras && (
            <div
              style={{
                padding: "0 14px 12px 14px",
                borderTop: "1px solid var(--border-weak)",
              }}
            >
              {expansionExtras}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
