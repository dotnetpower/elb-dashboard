import { useState, useCallback } from "react";
// import { useQuery } from "@tanstack/react-query";

import { ConfigBar } from "@/components/ConfigBar";
import { SetupWizard, loadSavedConfig, saveConfig, clearConfig, type ResourceConfig } from "@/components/SetupWizard";
import { SettingsPanel } from "@/components/SettingsPanel";
// import { GettingStarted } from "@/components/GettingStarted";
import { ClusterCard } from "@/components/cards/ClusterCard";
import { StorageCard } from "@/components/cards/StorageCard";
import { AcrCard } from "@/components/cards/AcrCard";
import { TerminalCard } from "@/components/cards/TerminalCard";
import { JobCard } from "@/components/cards/JobCard";
// import { monitoringApi, blastApi } from "@/api/endpoints";

export type MonitoringConfig = ResourceConfig;

export function Dashboard() {
  const [showWizard, setShowWizard] = useState(() => !loadSavedConfig());
  const [showSettings, setShowSettings] = useState(false);
  const [config, setConfig] = useState<ResourceConfig>(() => {
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

  // const enabled = Boolean(config.subscriptionId && config.workloadResourceGroup);
  // GettingStarted queries — temporarily disabled
  // const aksQuery = useQuery({
  //   queryKey: ["gs-aks", config.subscriptionId, config.workloadResourceGroup],
  //   queryFn: () => monitoringApi.aks(config.subscriptionId, config.workloadResourceGroup),
  //   enabled,
  //   staleTime: 120_000,
  // });
  // const storageQuery = useQuery({
  //   queryKey: ["gs-storage", config.subscriptionId, config.workloadResourceGroup, config.storageAccountName],
  //   queryFn: () => monitoringApi.storage(config.subscriptionId, config.workloadResourceGroup, config.storageAccountName),
  //   enabled: enabled && Boolean(config.storageAccountName),
  //   staleTime: 120_000,
  // });
  // const acrQuery = useQuery({
  //   queryKey: ["gs-acr", config.subscriptionId, config.acrResourceGroup, config.acrName],
  //   queryFn: () => monitoringApi.acr(config.subscriptionId, config.acrResourceGroup, config.acrName),
  //   enabled: enabled && Boolean(config.acrResourceGroup && config.acrName),
  //   staleTime: 120_000,
  // });
  // const jobsQuery = useQuery({
  //   queryKey: ["gs-jobs"],
  //   queryFn: () => blastApi.listJobs(),
  //   staleTime: 120_000,
  // });

  if (showWizard) {
    return <SetupWizard onComplete={handleWizardComplete} />;
  }

  return (
    <>
      <ConfigBar
        config={config}
        onChange={(next) => { setConfig(next); saveConfig(next); }}
        onOpenSettings={() => setShowSettings(true)}
      />

      {/* GettingStarted temporarily disabled
      <GettingStarted
        config={config}
        hasCluster={aksQuery.data?.clusters && aksQuery.data.clusters.length > 0}
        hasStorage={storageQuery.data?.containers && storageQuery.data.containers.length > 0}
        hasAcr={acrQuery.data?.actual_tags && Object.keys(acrQuery.data.actual_tags).length > 0}
        jobCount={jobsQuery.data?.jobs?.length}
      />
      */}

      <div className="page-header">
        <div className="page-header__title">Dashboard</div>
        <div className="page-header__desc">Your BLAST workspace at a glance</div>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <div className="dashboard-grid">
          <ClusterCard
            subscriptionId={config.subscriptionId}
            resourceGroup={config.workloadResourceGroup}
            region={config.region}
            acrResourceGroup={config.acrResourceGroup}
            acrName={config.acrName}
            storageResourceGroup={config.workloadResourceGroup}
            storageAccount={config.storageAccountName}
          />
          <AcrCard
            subscriptionId={config.subscriptionId}
            resourceGroup={config.acrResourceGroup}
            registryName={config.acrName}
          />
          <StorageCard
            subscriptionId={config.subscriptionId}
            resourceGroup={config.workloadResourceGroup}
            accountName={config.storageAccountName}
          />
          <TerminalCard
            subscriptionId={config.subscriptionId}
            resourceGroup={config.terminalResourceGroup}
            vmName={config.terminalVmName}
          />
        </div>

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
