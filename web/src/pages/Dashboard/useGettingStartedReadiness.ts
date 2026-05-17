import { useCallback, useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { monitoringApi } from "@/api/endpoints";
import type { ResourceConfig } from "@/components/SetupWizard";
import { useTerminalSidecarHealth } from "@/hooks/usePrerequisites";
import { usePrefetchApiReference } from "@/hooks/usePrefetchApiReference";

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

  const terminalSidecar = useTerminalSidecarHealth();

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
  const hasTerminal = terminalSidecar.isHealthy;
  const needsSetup =
    hasConfig &&
    !gettingStartedDismissed &&
    (!hasCluster || !hasImages || !hasTerminal);
  const queriesLoaded =
    aksQuery.isFetched && acrQuery.isFetched && !terminalSidecar.isLoading;

  useEffect(() => {
    if (queriesLoaded && needsSetup && !showGettingStarted && !showWizard) {
      setShowGettingStarted(true);
    }
  }, [queriesLoaded, needsSetup, showGettingStarted, showWizard]);

  const handleDismissGettingStarted = useCallback(() => {
    setShowGettingStarted(false);
    setGettingStartedDismissed(true);
    sessionStorage.setItem("elb-getting-started-dismissed", "true");
  }, []);

  const reopenGettingStarted = useCallback(() => {
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
    aksQuery,
  } as const;
}
