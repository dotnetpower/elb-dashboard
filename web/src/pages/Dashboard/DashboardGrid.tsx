import { AcrCard } from "@/components/cards/AcrCard";
import { ClusterCard } from "@/components/cards/ClusterCard";
import { JobCard } from "@/components/cards/JobCard";
import { SidecarsCard } from "@/components/cards/SidecarsCard";
import { StorageCard } from "@/components/cards/StorageCard";
import { TerminalCard } from "@/components/cards/TerminalCard";
import type { ResourceConfig } from "@/components/SetupWizard";
import { isFeatureEnabled } from "@/config/runtime";

export interface DashboardGridProps {
  config: ResourceConfig;
}

export function DashboardGrid({ config }: DashboardGridProps) {
  const terminalEnabled = isFeatureEnabled("terminal");

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {/*
        ClusterCard now owns its own row.  The bento layout inside has
        7 cells across 3 columns plus a 4-row activity rail; squeezing
        it into a 2-column dashboard grid produced unreadable wraps on
        anything narrower than ~1600px.  ACR / Storage / Terminal stay
        in the dense grid below — they each carry far less data per
        card.
      */}
      <ClusterCard
        subscriptionId={config.subscriptionId}
        resourceGroup={config.workloadResourceGroup}
        region={config.region}
        acrResourceGroup={config.acrResourceGroup}
        acrName={config.acrName}
        storageResourceGroup={config.workloadResourceGroup}
        storageAccount={config.storageAccountName}
        terminalResourceGroup={config.terminalResourceGroup}
        terminalVmName={config.terminalVmName}
      />

      <div className="dashboard-grid">
        <AcrCard
          subscriptionId={config.subscriptionId}
          resourceGroup={config.acrResourceGroup}
          registryName={config.acrName}
        />
        <StorageCard
          subscriptionId={config.subscriptionId}
          resourceGroup={config.workloadResourceGroup}
          accountName={config.storageAccountName}
          clusterName="elb-cluster"
          acrName={config.acrName}
        />
        {terminalEnabled && <TerminalCard />}
      </div>

      <SidecarsCard />

      <JobCard />
    </div>
  );
}
