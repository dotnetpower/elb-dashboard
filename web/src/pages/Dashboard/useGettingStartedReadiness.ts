import { useCallback, useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { monitoringApi } from "@/api/endpoints";
import type { ResourceConfig } from "@/components/SetupWizard";
import { useTerminalSidecarHealth } from "@/hooks/usePrerequisites";
import { usePrefetchApiReference } from "@/hooks/usePrefetchApiReference";
import { isFeatureEnabled } from "@/config/runtime";

const MOBILE_MEDIA_QUERY = "(max-width: 760px)";

function useIsMobileViewport(): boolean {
  const [isMobile, setIsMobile] = useState(() => {
    if (typeof window === "undefined" || !window.matchMedia) return false;
    return window.matchMedia(MOBILE_MEDIA_QUERY).matches;
  });
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia(MOBILE_MEDIA_QUERY);
    const onChange = (e: MediaQueryListEvent) => setIsMobile(e.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);
  return isMobile;
}

export interface UseGettingStartedReadinessArgs {
  config: ResourceConfig;
  showWizard: boolean;
}

/**
 * Detects whether the workspace is partially set up (config saved but
 * cluster / images / terminal sidecar not all healthy) and auto-opens
 * the Getting Started checklist exactly once per session.
 */
export function useGettingStartedReadiness({
  config,
  showWizard,
}: UseGettingStartedReadinessArgs) {
  const [showGettingStarted, setShowGettingStarted] = useState(false);
  const [gettingStartedDismissed, setGettingStartedDismissed] = useState(
    () => sessionStorage.getItem("elb-getting-started-dismissed") === "true",
  );
  const isMobile = useIsMobileViewport();

  const hasConfig = Boolean(
    config.subscriptionId &&
      config.workloadResourceGroup &&
      config.acrName &&
      config.storageAccountName,
  );

  // Subscription-wide list (matches ClusterCard / StorageCard) so a cluster
  // created in a non-anchor RG — the elastic-blast default — is detected and
  // the Getting Started panel does not pop "Create AKS" as a false negative.
  const aksQuery = useQuery({
    queryKey: ["aks", config.subscriptionId, "sub"],
    queryFn: () => monitoringApi.aks(config.subscriptionId),
    enabled: hasConfig && !gettingStartedDismissed,
    staleTime: 60_000,
    retry: 1,
  });

  const acrQuery = useQuery({
    queryKey: [
      "acr",
      config.subscriptionId,
      config.acrResourceGroup,
      config.acrName,
    ],
    queryFn: () =>
      monitoringApi.acr(
        config.subscriptionId,
        config.acrResourceGroup,
        config.acrName,
      ),
    enabled: hasConfig && !gettingStartedDismissed,
    staleTime: 60_000,
    retry: 1,
  });

  const terminalEnabled = isFeatureEnabled("terminal");
  const terminalSidecar = useTerminalSidecarHealth(terminalEnabled);

  // Pre-warm the React Query cache for the API Reference page so the
  // user does not have to sit through the "Discovering OpenAPI service
  // on AKS..." spinner when they navigate from here to /docs.
  usePrefetchApiReference({
    subscriptionId: config.subscriptionId,
    workloadResourceGroup: config.workloadResourceGroup,
    acrResourceGroup: config.acrResourceGroup,
    acrName: config.acrName,
  });

  const hasCluster = (aksQuery.data?.clusters?.length ?? 0) > 0;
  const hasImages = acrQuery.data?.actual_tags
    ? Object.keys(acrQuery.data.actual_tags).length >= 4
    : false;
  const hasTerminal = terminalEnabled ? terminalSidecar.isHealthy : true;
  // Only treat a prerequisite as "missing" when its probe actually
  // returned a usable answer. Errored / unreachable probes give a
  // false `hasCluster=false / hasImages=false` signal that would
  // otherwise pop the Getting Started modal whenever the backend is
  // unhealthy — turning a server outage into a misleading "you haven't
  // set this up yet" UX. Treat unknown signals as conservatively true.
  const aksProbeUsable = aksQuery.isSuccess;
  const acrProbeUsable = acrQuery.isSuccess;
  const terminalProbeUsable =
    !terminalEnabled ||
    terminalSidecar.status === "ok" ||
    terminalSidecar.status === "degraded" ||
    terminalSidecar.status === "down";
  const probesUsable = aksProbeUsable && acrProbeUsable && terminalProbeUsable;
  const clusterMissing = aksProbeUsable && !hasCluster;
  const imagesMissing = acrProbeUsable && !hasImages;
  const terminalMissing = terminalEnabled && terminalProbeUsable && !hasTerminal;
  const needsSetup =
    hasConfig &&
    !gettingStartedDismissed &&
    probesUsable &&
    (clusterMissing || imagesMissing || terminalMissing);
  const queriesLoaded = probesUsable;

  useEffect(() => {
    if (isMobile) return;
    if (queriesLoaded && needsSetup && !showGettingStarted && !showWizard) {
      setShowGettingStarted(true);
    }
  }, [isMobile, queriesLoaded, needsSetup, showGettingStarted, showWizard]);

  useEffect(() => {
    if (isMobile && showGettingStarted) {
      setShowGettingStarted(false);
    }
  }, [isMobile, showGettingStarted]);

  const handleDismissGettingStarted = useCallback(() => {
    setShowGettingStarted(false);
    setGettingStartedDismissed(true);
    sessionStorage.setItem("elb-getting-started-dismissed", "true");
  }, []);

  const reopenGettingStarted = useCallback(() => {
    if (typeof window !== "undefined" && window.matchMedia &&
        window.matchMedia(MOBILE_MEDIA_QUERY).matches) {
      return;
    }
    sessionStorage.removeItem("elb-getting-started-dismissed");
    setGettingStartedDismissed(false);
    setShowGettingStarted(true);
  }, []);

  return {
    showGettingStarted,
    gettingStartedDismissed,
    handleDismissGettingStarted,
    reopenGettingStarted,
    hasCluster,
    hasImages,
    hasTerminal,
    terminalEnabled,
    aksQuery,
  } as const;
}
