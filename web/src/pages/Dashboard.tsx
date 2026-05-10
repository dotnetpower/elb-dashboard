import { useState, useCallback, useEffect } from "react";
import { useQuery } from "@tanstack/react-query";

import { ConfigBar } from "@/components/ConfigBar";
import { SetupWizard, loadSavedConfig, saveConfig, clearConfig, type ResourceConfig } from "@/components/SetupWizard";
import { SettingsPanel } from "@/components/SettingsPanel";
import { ClusterCard } from "@/components/cards/ClusterCard";
import { StorageCard } from "@/components/cards/StorageCard";
import { AcrCard } from "@/components/cards/AcrCard";
import { TerminalCard } from "@/components/cards/TerminalCard";
import { JobCard } from "@/components/cards/JobCard";
import { armProxyApi } from "@/api/endpoints";
import { Loader2, Search } from "lucide-react";

export type MonitoringConfig = ResourceConfig;

/** Try to build a ResourceConfig from elb-* tags on a resource group. */
function configFromTags(
  subscriptionId: string,
  rg: { name: string; location: string; tags?: Record<string, string> },
): ResourceConfig | null {
  const t = rg.tags ?? {};
  // Must have at least one elb- tag to qualify
  const hasElb = Object.keys(t).some((k) => k.startsWith("elb-"));
  if (!hasElb) return null;
  return {
    subscriptionId,
    workloadResourceGroup: rg.name,
    acrResourceGroup: t["elb-acr-rg"] || rg.name,
    acrName: t["elb-acr"] || "",
    storageAccountName: t["elb-storage"] || "",
    terminalResourceGroup: t["elb-terminal-rg"] || "rg-elb-terminal",
    terminalVmName: t["elb-terminal-vm"] || "vm-elb-terminal",
    region: t["elb-region"] || rg.location || "koreacentral",
  };
}

export function Dashboard() {
  const hasSaved = loadSavedConfig();
  // A saved config is "complete" only if it has ACR + Storage configured
  const savedIsComplete = !!(hasSaved?.acrName && hasSaved?.storageAccountName);
  const [showWizard, setShowWizard] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [discoveryDone, setDiscoveryDone] = useState(savedIsComplete);
  const [discoveredWorkspaces, setDiscoveredWorkspaces] = useState<
    { config: ResourceConfig; rgName: string }[]
  >([]);

  const [config, setConfig] = useState<ResourceConfig>(() => {
    // Only use saved config if it's complete
    if (savedIsComplete && hasSaved) return hasSaved;
    return {
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

  // --- Auto-discovery: fetch subs → RGs → scan elb-* tags ---
  const needsDiscovery = !savedIsComplete && !discoveryDone;

  const subsQuery = useQuery({
    queryKey: ["auto-discover-subs"],
    queryFn: armProxyApi.listSubscriptions,
    enabled: needsDiscovery,
    staleTime: 5 * 60_000,
    retry: 1,
  });

  // Fetch RGs for every subscription
  const rgsQueries = useQuery({
    queryKey: ["auto-discover-rgs", subsQuery.data?.map((s) => s.subscriptionId)],
    queryFn: async () => {
      const subs = subsQuery.data ?? [];
      const results: { subscriptionId: string; rgs: { name: string; location: string; tags?: Record<string, string> }[] }[] = [];
      for (const sub of subs) {
        try {
          const rgs = await armProxyApi.listResourceGroups(sub.subscriptionId);
          results.push({ subscriptionId: sub.subscriptionId, rgs });
        } catch { /* skip inaccessible subs */ }
      }
      return results;
    },
    enabled: needsDiscovery && !!subsQuery.data?.length,
    staleTime: 5 * 60_000,
    retry: 1,
  });

  // Process discovery results
  useEffect(() => {
    if (!needsDiscovery || !rgsQueries.data) return;
    const found: { config: ResourceConfig; rgName: string }[] = [];
    for (const { subscriptionId, rgs } of rgsQueries.data) {
      for (const rg of rgs) {
        const cfg = configFromTags(subscriptionId, rg);
        if (cfg) found.push({ config: cfg, rgName: rg.name });
      }
    }
    if (found.length === 1) {
      // Single workspace — auto-apply
      setConfig(found[0].config);
      saveConfig(found[0].config);
      setDiscoveryDone(true);
    } else if (found.length > 1) {
      // Multiple workspaces — let user pick
      setDiscoveredWorkspaces(found);
      setDiscoveryDone(true);
    } else {
      // No workspace found — show wizard
      setDiscoveryDone(true);
      setShowWizard(true);
    }
  }, [needsDiscovery, rgsQueries.data]);

  // Also open wizard if discovery fails
  useEffect(() => {
    if (!needsDiscovery) return;
    if (subsQuery.isError || rgsQueries.isError) {
      setDiscoveryDone(true);
      setShowWizard(true);
    }
  }, [needsDiscovery, subsQuery.isError, rgsQueries.isError]);

  const handleWizardComplete = useCallback((wizConfig: ResourceConfig) => {
    setConfig(wizConfig);
    setShowWizard(false);
    setDiscoveredWorkspaces([]);
  }, []);

  const handleRerunWizard = useCallback(() => {
    clearConfig();
    setShowSettings(false);
    setShowWizard(true);
    setDiscoveredWorkspaces([]);
  }, []);

  const handlePickWorkspace = useCallback((ws: ResourceConfig) => {
    setConfig(ws);
    saveConfig(ws);
    setDiscoveredWorkspaces([]);
  }, []);

  // --- Discovery loading screen ---
  if (!discoveryDone) {
    return (
      <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", minHeight: "60vh", gap: 16 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <Search size={20} style={{ color: "var(--accent)" }} />
          <Loader2 size={20} className="spin" style={{ color: "var(--accent)" }} />
        </div>
        <div style={{ fontSize: 14, color: "var(--text-primary)" }}>Discovering existing BLAST workspaces…</div>
        <div className="muted" style={{ fontSize: 12 }}>Scanning resource groups for workspace configuration</div>
      </div>
    );
  }

  // --- Multiple workspaces found — picker ---
  if (discoveredWorkspaces.length > 1) {
    return (
      <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", minHeight: "60vh", gap: 20, padding: "0 32px" }}>
        <div>
          <h2 style={{ fontSize: 18, fontWeight: 700, textAlign: "center", margin: 0 }}>BLAST Workspaces Found</h2>
          <div className="muted" style={{ fontSize: 12, textAlign: "center", marginTop: 4 }}>
            {discoveredWorkspaces.length} existing workspaces detected. Choose one to continue.
          </div>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 8, width: "100%", maxWidth: 480 }}>
          {discoveredWorkspaces.map((ws) => (
            <button
              key={`${ws.config.subscriptionId}/${ws.rgName}`}
              onClick={() => handlePickWorkspace(ws.config)}
              className="glass-card"
              style={{
                display: "flex", flexDirection: "column", gap: 4, padding: "14px 18px",
                border: "1px solid var(--border-medium)", borderRadius: 10,
                background: "var(--glass-bg)", cursor: "pointer", textAlign: "left",
                transition: "border-color 0.15s, background 0.15s",
              }}
              onMouseEnter={(e) => { e.currentTarget.style.borderColor = "var(--accent)"; e.currentTarget.style.background = "var(--glass-bg-strong)"; }}
              onMouseLeave={(e) => { e.currentTarget.style.borderColor = "var(--border-medium)"; e.currentTarget.style.background = "var(--glass-bg)"; }}
            >
              <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)" }}>{ws.rgName}</div>
              <div className="muted" style={{ fontSize: 11, display: "flex", gap: 12, flexWrap: "wrap" }}>
                {ws.config.storageAccountName && <span>Storage: {ws.config.storageAccountName}</span>}
                {ws.config.acrName && <span>ACR: {ws.config.acrName}</span>}
                <span>Region: {ws.config.region}</span>
              </div>
            </button>
          ))}
        </div>
        <button
          onClick={() => setShowWizard(true)}
          style={{ background: "none", border: "none", color: "var(--accent)", cursor: "pointer", fontSize: 12, marginTop: 4 }}
        >
          Or set up a new workspace →
        </button>
      </div>
    );
  }

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
