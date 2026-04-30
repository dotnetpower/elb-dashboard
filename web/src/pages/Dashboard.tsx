import { useState } from "react";

import { ConfigBar } from "@/components/ConfigBar";
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
}

const DEFAULT_CONFIG: MonitoringConfig = {
  subscriptionId: "",
  workloadResourceGroup: "",
  acrResourceGroup: "",
  acrName: "",
  storageAccountName: "",
  terminalResourceGroup: "",
  terminalVmName: "",
};

export function Dashboard() {
  const [config, setConfig] = useState<MonitoringConfig>(DEFAULT_CONFIG);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-5)" }}>
      <header>
        <h1 style={{ margin: 0 }}>Dashboard</h1>
        <p className="muted" style={{ marginTop: "var(--space-2)" }}>
          Live state of your ElasticBLAST resources. Polled every 30 seconds.
        </p>
      </header>

      <ConfigBar config={config} onChange={setConfig} />

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(360px, 1fr))",
          gap: "var(--space-5)",
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
        <JobCard />
      </div>
    </div>
  );
}
