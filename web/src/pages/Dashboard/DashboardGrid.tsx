import { AcrCard } from "@/components/cards/AcrCard";
import { ClusterCard } from "@/components/cards/ClusterCard";
import { SidecarsCard } from "@/components/cards/SidecarsCard";
import { StorageCard } from "@/components/cards/StorageCard";
import { TerminalCard } from "@/components/cards/TerminalCard";
import type { ResourceConfig } from "@/components/SetupWizard";
import { usePreviewFeatureEnabled } from "@/hooks/usePreferences";
import { useScrollToHash } from "@/hooks/useScrollToHash";

export interface DashboardGridProps {
  config: ResourceConfig;
}

export function DashboardGrid({ config }: DashboardGridProps) {
  const terminalEnabled = usePreviewFeatureEnabled("terminal");
  // Mounted only after workspace discovery completes, so `#acr-card`
  // (and any future card anchors) are guaranteed to be in the DOM when
  // the deep-link scroll fires. Putting this in `Dashboard` would race
  // the DiscoveryLoading splash and silently no-op.
  useScrollToHash();

  return (
    <div className="dashboard-workspace">
      {/*
        ClusterCard now owns its own row.  The bento layout inside has
        7 cells across 3 columns plus a 4-row activity rail; squeezing
        it into a 2-column dashboard grid produced unreadable wraps on
        anything narrower than ~1600px.  ACR / Storage / Terminal stay
        in the dense grid below — they each carry far less data per
        card.
      */}
      <section
        className="dashboard-workspace__section dashboard-workspace__section--cluster"
        aria-label="Cluster overview"
      >
        <div className="dashboard-section-label">
          <span className="dashboard-section-label__dot dashboard-section-label__dot--cluster" />
          Cluster plane
        </div>
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
      </section>

      <section className="dashboard-workspace__section" aria-label="Resource plane">
        <div className="dashboard-section-label">
          <span className="dashboard-section-label__dot dashboard-section-label__dot--resource" />
          Resource plane
        </div>
        <div className="dashboard-grid dashboard-grid--resources">
          <div id="acr-card">
            <AcrCard
              subscriptionId={config.subscriptionId}
              resourceGroup={config.acrResourceGroup}
              registryName={config.acrName}
            />
          </div>
          <StorageCard
            subscriptionId={config.subscriptionId}
            resourceGroup={config.workloadResourceGroup}
            accountName={config.storageAccountName}
            clusterName="elb-cluster"
            acrName={config.acrName}
          />
          {terminalEnabled && (
            <div className="dashboard-hide-mobile">
              <TerminalCard />
            </div>
          )}
        </div>
      </section>

      <section
        className="dashboard-workspace__section dashboard-hide-mobile"
        aria-label="Sidecar runtime"
      >
        <div className="dashboard-section-label">
          <span className="dashboard-section-label__dot dashboard-section-label__dot--runtime" />
          Sidecar runtime
        </div>
        <SidecarsCard />
      </section>
    </div>
  );
}
