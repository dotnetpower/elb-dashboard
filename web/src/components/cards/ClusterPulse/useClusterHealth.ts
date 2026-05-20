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
  statusTone: HealthTone;
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

  const status = useMemo<{ statusLine: string; statusTone: HealthTone }>(() => {
    if (trans === "starting") {
      return { statusLine: "Starting cluster…", statusTone: "transitioning" };
    }
    if (trans === "stopping") {
      return { statusLine: "Stopping cluster…", statusTone: "transitioning" };
    }
    if (provisioningBusy) {
      const label = getAksProvisioningLabel(c);
      return {
        statusLine: label ? `${label}…` : "Provisioning…",
        statusTone: "transitioning",
      };
    }
    if (isAksProvisioningFailed(c)) {
      return { statusLine: "Provisioning failed", statusTone: "degraded" };
    }
    if (!isRunning) {
      return { statusLine: "Cluster is stopped", statusTone: "down" };
    }
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
      return {
        statusLine: parts.length > 0 ? parts.join(" · ") : "Degraded",
        statusTone: "degraded",
      };
    }
    if (tone === "unknown") {
      return { statusLine: "Metrics not yet available", statusTone: "unknown" };
    }
    const softParts: string[] = [];
    if (apiP95 != null && apiP95 > 2000)
      softParts.push(`API p95 ${fmtMs(apiP95)}`);
    if (apiErrors > 0) softParts.push(`${apiErrors} errors / 15m`);
    if (softParts.length > 0) {
      return { statusLine: softParts.join(" · "), statusTone: "degraded" };
    }
    return { statusLine: "All systems nominal", statusTone: "healthy" };
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

  return { tone, statusLine: status.statusLine, statusTone: status.statusTone };
}
