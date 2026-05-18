/**
 * useClusterHealth — pure verdict + status-line derivation.
 *
 * Inputs are already-resolved signals from `usePulseSignals` plus
 * lifecycle predicates from `@/utils/aksStatus`. Output is the row's
 * health tone and the human-readable status string under the cluster
 * name. Kept side-effect free so it can be unit-tested.
 */

import { useMemo } from "react";

import type { AksClusterSummary } from "@/api/endpoints";
import {
  getAksProvisioningLabel,
  isAksProvisioningFailed,
} from "@/utils/aksStatus";

import { fmtMs, type HealthTone } from "./helpers";

export interface ClusterHealthInput {
  cluster: AksClusterSummary;
  isRunning: boolean;
  isTransitioning: boolean;
  provisioningBusy: boolean;
  trans?: "starting" | "stopping";
  cpuPct: number | null;
  memPct: number | null;
  apiP95: number | null;
  apiErrors: number;
  jobsDegraded: boolean;
  metricsDegraded: boolean;
  nodeNotReady: number;
  nodePressureCount: number;
  nodeTotal: number;
}

export interface ClusterHealthVerdict {
  tone: HealthTone;
  statusLine: string;
}

export function useClusterHealth(
  input: ClusterHealthInput,
): ClusterHealthVerdict {
  const {
    cluster: c,
    isRunning,
    isTransitioning,
    provisioningBusy,
    trans,
    cpuPct,
    memPct,
    apiP95,
    apiErrors,
    jobsDegraded,
    metricsDegraded,
    nodeNotReady,
    nodePressureCount,
    nodeTotal,
  } = input;

  const tone = useMemo<HealthTone>(() => {
    if (isTransitioning || provisioningBusy) return "transitioning";
    if (isAksProvisioningFailed(c)) return "degraded";
    if (!isRunning) return "down";
    if (c.power_state && c.power_state !== "Running") return "down";
    if (nodeNotReady > 0 || nodePressureCount > 0) return "degraded";
    if (cpuPct != null && cpuPct >= 0.95) return "degraded";
    if (memPct != null && memPct >= 0.95) return "degraded";
    if (apiP95 != null && apiP95 > 2000) return "degraded";
    if (apiErrors > 5) return "degraded";
    if (
      jobsDegraded &&
      metricsDegraded &&
      nodeTotal === 0 &&
      cpuPct == null &&
      memPct == null
    ) {
      return "unknown";
    }
    return "healthy";
  }, [
    c,
    isRunning,
    isTransitioning,
    provisioningBusy,
    cpuPct,
    memPct,
    apiP95,
    apiErrors,
    jobsDegraded,
    metricsDegraded,
    nodeNotReady,
    nodePressureCount,
    nodeTotal,
  ]);

  const statusLine = useMemo(() => {
    if (trans === "starting") return "Starting cluster…";
    if (trans === "stopping") return "Stopping cluster…";
    if (provisioningBusy) {
      const label = getAksProvisioningLabel(c);
      return label ? `${label}…` : "Provisioning…";
    }
    if (isAksProvisioningFailed(c)) return "Provisioning failed";
    if (!isRunning) return "Cluster is stopped";
    if (tone === "degraded") {
      const parts: string[] = [];
      if (cpuPct != null && cpuPct >= 0.85)
        parts.push(`CPU ${Math.round(cpuPct * 100)}%`);
      if (memPct != null && memPct >= 0.85)
        parts.push(`Mem ${Math.round(memPct * 100)}%`);
      if (apiP95 != null && apiP95 > 2000)
        parts.push(`API p95 ${fmtMs(apiP95)}`);
      if (apiErrors > 0) parts.push(`${apiErrors} errors / 15m`);
      if (nodeNotReady > 0) parts.push(`${nodeNotReady} node not ready`);
      return parts.length > 0 ? parts.join(" · ") : "Degraded";
    }
    if (tone === "unknown") return "Metrics not yet available";
    return "All systems nominal";
  }, [
    c,
    trans,
    provisioningBusy,
    isRunning,
    tone,
    cpuPct,
    memPct,
    apiP95,
    apiErrors,
    nodeNotReady,
  ]);

  return { tone, statusLine };
}
