import { useState, useCallback } from "react";

import { ConfigBar } from "@/components/ConfigBar";
import { SetupWizard, loadSavedConfig, clearConfig, type ResourceConfig } from "@/components/SetupWizard";
import { SettingsPanel } from "@/components/SettingsPanel";
import { ClusterCard } from "@/components/cards/ClusterCard";
import { StorageCard } from "@/components/cards/StorageCard";
import { AcrCard } from "@/components/cards/AcrCard";
import { TerminalCard } from "@/components/cards/TerminalCard";
import { JobCard } from "@/components/cards/JobCard";

export interface MonitoringConfig {
  subscriptionId: string;
  workloadResourceGroup: string;
  acrResourceGroup: string;
  acrName: string;
  storageAccountName: string;
  terminalResourceGroup: string;
  terminalVmName: string;
  region: string;
}

export function Dashboard() {
  const [showWizard, setShowWizard] = useState(() => {
    const saved = loadSavedConfig();
    // eslint-disable-next-line no-console
    console.log("[Dashboard] loadSavedConfig:", saved);
    return !saved;
  });
  const [showSettings, setShowSettings] = useState(false);
  const [config, setConfig] = useState<MonitoringConfig>(() => {
    const saved = loadSavedConfig();
    return saved ?? {
      subscriptionId: "",
      workloadResourceGroup: "",
      acrResourceGroup: "",
      acrName: "",
      storageAccountName: "",
      terminalResourceGroup: "rg-elb-terminal",
      terminalVmName: "vm-elb-terminal",
      region: "koreacentral",
    };
  });

  const handleWizardComplete = useCallback((wizConfig: ResourceConfig) => {
    setConfig(wizConfig);
    setShowWizard(false);
  }, []);

  const handleRerunWizard = useCallback(() => {
    clearConfig();
    setShowSettings(false);
    setShowWizard(true);
  }, []);

  if (showWizard) {
    return <SetupWizard onComplete={handleWizardComplete} />;
  }

  return (
    <>
      <ConfigBar
        config={config}
        onChange={setConfig}
        onOpenSettings={() => setShowSettings(true)}
      />

      <div style={{ padding: "16px 20px", display: "flex", flexDirection: "column", gap: 12 }}>
        {/* Panel grid */}
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(340px, 1fr))",
            gap: 8,
          }}
        >
          <ClusterCard
            subscriptionId={config.subscriptionId}
            resourceGroup={config.workloadResourceGroup}
          />
          <StorageCard
            subscriptionId={config.subscriptionId}
            resourceGroup={config.workloadResourceGroup}
            accountName={config.storageAccountName}
          />
          <AcrCard
            subscriptionId={config.subscriptionId}
            resourceGroup={config.acrResourceGroup}
            registryName={config.acrName}
          />
          <TerminalCard
            subscriptionId={config.subscriptionId}
            resourceGroup={config.terminalResourceGroup}
            vmName={config.terminalVmName}
          />
        </div>

        {/* Jobs — full width */}
        <JobCard />
      </div>

      <SettingsPanel
        open={showSettings}
        config={config}
        onClose={() => setShowSettings(false)}
        onRerunWizard={handleRerunWizard}
      />
    </>
  );
}
