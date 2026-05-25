/**
 * Shared prerequisite hooks for action-button gating.
 *
 * - useClusterReadiness — knows whether an AKS cluster exists and whether
 *   at least one is in Running state. Used to gate "New search" /
 *   "Re-submit" entry points.
 * - useTerminalSidecarHealth — knows whether the in-process `terminal`
 *   sidecar (ttyd loopback at 127.0.0.1:7681) is reachable. Used to gate
 *   "Open Terminal" / "Check Terminal" entry points.
 *
 * Both hooks load configuration via loadSavedConfig() and share React Query
 * cache keys with the corresponding cards, so a single network call serves the
 * whole page. Cluster readiness intentionally uses the subscription-wide AKS
 * envelope because ElasticBLAST workload clusters may live outside the
 * dashboard anchor resource group.
 */
import { useQuery } from "@tanstack/react-query";

import { fetchApiRaw } from "@/api/client";
import { monitoringApi } from "@/api/endpoints";
import { loadSavedConfig } from "@/components/SetupWizard";
import { isAksWorkloadReady } from "@/utils/aksStatus";

export interface ClusterReadiness {
  /** A cluster resource is present in the workload RG. */
  hasAnyCluster: boolean;
  /** At least one cluster is provisioned and reports power_state === "Running". */
  hasRunningCluster: boolean;
  /** Query is still loading and the answer is unknown. */
  isLoading: boolean;
  /** Underlying query errored — treat as "unknown / not ready". */
  isError: boolean;
}

export function useClusterReadiness(): ClusterReadiness {
  const config = loadSavedConfig();
  const subId = config?.subscriptionId ?? "";
  const enabled = Boolean(subId);

  const query = useQuery({
    queryKey: ["aks", subId, "sub"],
    queryFn: () => monitoringApi.aks(subId),
    enabled,
    refetchInterval: 30_000,
    staleTime: 15_000,
  });

  const clusters = query.data?.clusters ?? [];
  return {
    hasAnyCluster: clusters.length > 0,
    hasRunningCluster: clusters.some(isAksWorkloadReady),
    isLoading: enabled && query.isLoading,
    isError: query.isError,
  };
}

interface TerminalHealthResponse {
  status: "ok" | "degraded" | "down";
  upstream_status?: number;
  error?: string;
}

export interface TerminalSidecarHealth {
  /** Sidecar reports ok and is reachable. */
  isHealthy: boolean;
  /** Status string from the API or "checking" / "unknown". */
  status: "ok" | "degraded" | "down" | "checking" | "unknown";
  isLoading: boolean;
}

async function fetchTerminalHealth(): Promise<TerminalHealthResponse> {
  const r = await fetchApiRaw("/terminal/health", { method: "GET" });
  if (!r.ok) {
    return { status: "down", error: `HTTP ${r.status}` };
  }
  return (await r.json()) as TerminalHealthResponse;
}

export function useTerminalSidecarHealth(enabled = true): TerminalSidecarHealth {
  const query = useQuery({
    queryKey: ["terminal-sidecar-health"],
    queryFn: fetchTerminalHealth,
    enabled,
    refetchInterval: 30_000,
    staleTime: 15_000,
    retry: false,
  });

  if (!enabled) {
    return {
      isHealthy: false,
      status: "unknown",
      isLoading: false,
    };
  }

  const status = query.data?.status ?? (query.isLoading ? "checking" : "unknown");
  return {
    isHealthy: status === "ok",
    status,
    isLoading: query.isLoading,
  };
}
