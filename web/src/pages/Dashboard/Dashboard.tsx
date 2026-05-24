import { useCallback } from "react";

import { GettingStartedGuide } from "@/components/GettingStartedGuide";
import { clearConfig, SetupWizard, type ResourceConfig } from "@/components/SetupWizard";
import { WorkspaceDiagnosticsBanner } from "@/components/WorkspaceDiagnosticsBanner";
import { useSettingsPanel } from "@/hooks/useSettingsPanel";
import { isAksWorkloadReady } from "@/utils/aksStatus";

import { DashboardGrid } from "./DashboardGrid";
import { DashboardHeader } from "./DashboardHeader";
import { DiscoveryLoading } from "./DiscoveryLoading";
import { useGettingStartedReadiness } from "./useGettingStartedReadiness";
import { useWorkspaceDiscovery } from "./useWorkspaceDiscovery";
import { WorkspacePicker } from "./WorkspacePicker";

export type MonitoringConfig = ResourceConfig;

export function Dashboard() {
  const {
    config,
    setConfig,
    discoveryDone,
    discoveredWorkspaces,
    showWizard,
    setShowWizard,
    handlePickWorkspace,
    skipDiscovery,
    setDiscoveredWorkspaces,
  } = useWorkspaceDiscovery();

  const { open: openSettings } = useSettingsPanel();

  const {
    showGettingStarted,
    gettingStartedDismissed,
    handleDismissGettingStarted,
    reopenGettingStarted,
    hasCluster,
    hasImages,
    hasTerminal,
    terminalEnabled,
    aksQuery,
  } = useGettingStartedReadiness({ config, showWizard });

  const handleWizardComplete = useCallback(
    (wizConfig: ResourceConfig) => {
      setConfig(wizConfig);
      setShowWizard(false);
      setDiscoveredWorkspaces([]);
    },
    [setConfig, setShowWizard, setDiscoveredWorkspaces],
  );

  const handleRerunWizard = useCallback(() => {
    clearConfig();
    setShowWizard(true);
    setDiscoveredWorkspaces([]);
  }, [setShowWizard, setDiscoveredWorkspaces]);

  if (!discoveryDone) {
    return <DiscoveryLoading onSkip={skipDiscovery} />;
  }

  if (discoveredWorkspaces.length > 1) {
    return (
      <WorkspacePicker
        workspaces={discoveredWorkspaces}
        onPick={handlePickWorkspace}
        onSetupNew={() => setShowWizard(true)}
      />
    );
  }

  if (showWizard) {
    return (
      <SetupWizard
        onComplete={handleWizardComplete}
        onClose={() => setShowWizard(false)}
      />
    );
  }

  return (
    <>
      <DashboardHeader
        config={config}
        setConfig={setConfig}
        gettingStartedDismissed={gettingStartedDismissed}
        onReopenGettingStarted={reopenGettingStarted}
        onOpenSettings={openSettings}
      />

      <WorkspaceDiagnosticsBanner
        config={config}
        onResetWorkspace={handleRerunWizard}
      />

      <DashboardGrid config={config} />

      {showGettingStarted && (
        <GettingStartedGuide
          hasCluster={hasCluster}
          hasImages={hasImages}
          hasTerminal={hasTerminal}
          terminalEnabled={terminalEnabled}
          clusterRunning={aksQuery.data?.clusters?.some(isAksWorkloadReady) ?? false}
          acrName={config.acrName}
          onDismiss={handleDismissGettingStarted}
        />
      )}
    </>
  );
}
