import { useCallback, useState } from "react";

import { GettingStartedGuide } from "@/components/GettingStartedGuide";
import { SettingsPanel } from "@/components/SettingsPanel";
import {
  clearConfig,
  SetupWizard,
  type ResourceConfig,
} from "@/components/SetupWizard";
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

  const [showSettings, setShowSettings] = useState(false);

  const {
    showGettingStarted,
    gettingStartedDismissed,
    handleDismissGettingStarted,
    reopenGettingStarted,
    hasCluster,
    hasImages,
    hasTerminal,
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
    setShowSettings(false);
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
        onOpenSettings={() => setShowSettings(true)}
      />

      <DashboardGrid config={config} />

      <SettingsPanel
        open={showSettings}
        config={config}
        onClose={() => setShowSettings(false)}
        onRerunWizard={handleRerunWizard}
      />

      {showGettingStarted && (
        <GettingStartedGuide
          hasCluster={hasCluster}
          hasImages={hasImages}
          hasTerminal={hasTerminal}
          clusterRunning={
            aksQuery.data?.clusters?.some(isAksWorkloadReady) ?? false
          }
          acrName={config.acrName}
          onDismiss={handleDismissGettingStarted}
        />
      )}
    </>
  );
}
