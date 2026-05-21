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

  const aksQuery = useQuery({
    queryKey: ["aks", config.subscriptionId, config.workloadResourceGroup],
    queryFn: () =>
      monitoringApi.aks(config.subscriptionId, config.workloadResourceGroup),
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
  const needsSetup =
    hasConfig &&
    !gettingStartedDismissed &&
    (!hasCluster || !hasImages || !hasTerminal);
  const queriesLoaded =
    aksQuery.isFetched &&
    acrQuery.isFetched &&
    (!terminalEnabled || !terminalSidecar.isLoading);

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
