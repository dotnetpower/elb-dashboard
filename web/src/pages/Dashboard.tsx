import { useState, useCallback, useEffect } from "react";
import { useQuery } from "@tanstack/react-query";

import { SubscriptionPicker } from "@/components/SubscriptionPicker";
import { ResourcePicker } from "@/components/ResourcePicker";
import { SetupWizard, loadSavedConfig, saveConfig, clearConfig, type ResourceConfig } from "@/components/SetupWizard";
import { SettingsPanel } from "@/components/SettingsPanel";
import { ClusterCard } from "@/components/cards/ClusterCard";
import { StorageCard } from "@/components/cards/StorageCard";
import { AcrCard } from "@/components/cards/AcrCard";
import { TerminalCard } from "@/components/cards/TerminalCard";
import { JobCard } from "@/components/cards/JobCard";
import { SidecarsCard } from "@/components/cards/SidecarsCard";
import { GettingStartedGuide } from "@/components/GettingStartedGuide";
import { armProxyApi, monitoringApi } from "@/api/endpoints";
import { listSubscriptions as armListSubs, listResourceGroups as armListRGs } from "@/api/arm";
import { useTerminalSidecarHealth } from "@/hooks/usePrerequisites";
import { usePrefetchApiReference } from "@/hooks/usePrefetchApiReference";
import { AUTO_REFRESH_OPTIONS, useAutoRefresh } from "@/hooks/useAutoRefresh";
import {
  HelpCircle,
  LayoutGrid,
  Loader2,
  RefreshCw,
  Search,
  Settings as SettingsIcon,
} from "lucide-react";

const DEV_BYPASS = import.meta.env.VITE_AUTH_DEV_BYPASS === "true";

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
  const [showGettingStarted, setShowGettingStarted] = useState(false);
  const [gettingStartedDismissed, setGettingStartedDismissed] = useState(
    () => sessionStorage.getItem("elb-getting-started-dismissed") === "true"
  );
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
    queryFn: async () => {
      if (DEV_BYPASS) return armProxyApi.listSubscriptions();
      // Try direct ARM call first (no OBO needed), fall back to backend proxy
      try {
        const subs = await armListSubs();
        return subs.map(s => ({ subscriptionId: s.subscriptionId, displayName: s.displayName }));
      } catch {
        return armProxyApi.listSubscriptions();
      }
    },
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
          const rgList = DEV_BYPASS
            ? await armProxyApi.listResourceGroups(sub.subscriptionId)
            : await armListRGs(sub.subscriptionId);
          const rgs = rgList.map(r => ({ name: r.name, location: r.location, tags: r.tags }));
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

  // Empty subscription list (e.g. dev-bypass without ARM creds, or a tenant
  // the caller has zero RBAC on) — there is nothing to scan, so skip
  // straight to the manual wizard instead of spinning forever on
  // "Discovering existing BLAST workspaces…". Without this, rgsQueries
  // stays disabled (needs subsQuery.data.length > 0) and the loading
  // screen never resolves.
  useEffect(() => {
    if (!needsDiscovery) return;
    if (subsQuery.isSuccess && (subsQuery.data?.length ?? 0) === 0) {
      setDiscoveryDone(true);
      setShowWizard(true);
    }
  }, [needsDiscovery, subsQuery.isSuccess, subsQuery.data]);

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

  // --- Workspace readiness detection for Getting Started guide ---
  const hasConfig = Boolean(config.subscriptionId && config.workloadResourceGroup && config.acrName && config.storageAccountName);

  const aksQuery = useQuery({
    queryKey: ["gs-aks", config.subscriptionId, config.workloadResourceGroup],
    queryFn: () => monitoringApi.aks(config.subscriptionId, config.workloadResourceGroup),
    enabled: hasConfig && !gettingStartedDismissed,
    staleTime: 60_000,
    retry: 1,
  });

  const acrQuery = useQuery({
    queryKey: ["gs-acr", config.subscriptionId, config.acrResourceGroup, config.acrName],
    queryFn: () => monitoringApi.acr(config.subscriptionId, config.acrResourceGroup, config.acrName),
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

  // Detect "needs setup" state: has base config but missing key resources
  const hasCluster = (aksQuery.data?.clusters?.length ?? 0) > 0;
  const hasImages = acrQuery.data?.actual_tags ? Object.keys(acrQuery.data.actual_tags).length >= 4 : false;
  const hasTerminal = terminalSidecar.isHealthy;
  const needsSetup = hasConfig && !gettingStartedDismissed && (!hasCluster || !hasImages || !hasTerminal);
  const queriesLoaded = aksQuery.isFetched && acrQuery.isFetched && !terminalSidecar.isLoading;

  // Auto-show Getting Started when workspace needs setup
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
        <button
          onClick={() => { setDiscoveryDone(true); setShowWizard(true); }}
          style={{ marginTop: 12, background: "none", border: "1px solid var(--border-medium)", borderRadius: 8, color: "var(--text-muted)", cursor: "pointer", padding: "6px 16px", fontSize: 12, transition: "all 0.15s" }}
          onMouseEnter={e => { e.currentTarget.style.borderColor = "var(--accent)"; e.currentTarget.style.color = "var(--accent)"; }}
          onMouseLeave={e => { e.currentTarget.style.borderColor = "var(--border-medium)"; e.currentTarget.style.color = "var(--text-muted)"; }}
        >
          Skip discovery — set up manually
        </button>
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
    return <SetupWizard onComplete={handleWizardComplete} onClose={() => setShowWizard(false)} />;
  }

  return (
    <>
      <header
        className="page-header"
        style={{
          display: "flex",
          flexDirection: "column",
          gap: 8,
          marginBottom: 0,
        }}
      >
        {/* Row 1 — title (left) + workspace + auto-refresh + buttons (right). */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 12,
            flexWrap: "wrap",
          }}
        >
          <div
            className="page-header__title"
            style={{ display: "flex", alignItems: "center", gap: 10 }}
          >
            <LayoutGrid size={22} strokeWidth={1.5} style={{ color: "var(--accent)" }} />
            ElasticBLAST Dashboard
          </div>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
              flexWrap: "wrap",
              justifyContent: "flex-end",
            }}
          >
            <SubscriptionPicker
              value={config.subscriptionId}
              onChange={(id) => {
                const next = { ...config, subscriptionId: id };
                setConfig(next);
                saveConfig(next);
              }}
              compact
              style={{ maxWidth: 240, minWidth: 160 }}
            />
            <ResourcePicker
              label="Workload RG"
              value={config.workloadResourceGroup}
              onChange={async (v) => {
                const next = { ...config, workloadResourceGroup: v };
                if (config.subscriptionId && v) {
                  try {
                    const { tags } = await armProxyApi.getRgTags(config.subscriptionId, v);
                    if (tags["elb-acr-rg"]) next.acrResourceGroup = tags["elb-acr-rg"];
                    if (tags["elb-acr"]) next.acrName = tags["elb-acr"];
                    if (tags["elb-storage"]) next.storageAccountName = tags["elb-storage"];
                    if (tags["elb-terminal-rg"]) next.terminalResourceGroup = tags["elb-terminal-rg"];
                    if (tags["elb-terminal-vm"]) next.terminalVmName = tags["elb-terminal-vm"];
                    if (tags["elb-region"]) next.region = tags["elb-region"];
                  } catch {
                    /* RG has no elb-* tags — leave the rest of the config untouched. */
                  }
                }
                setConfig(next);
                saveConfig(next);
              }}
              queryKey={["arm-rgs", config.subscriptionId]}
              fetcher={
                config.subscriptionId
                  ? async () => {
                      const groups = await armProxyApi.listResourceGroups(config.subscriptionId);
                      const items = groups.map((g) => {
                        const tags = g.tags ?? {};
                        const isElb = Object.keys(tags).some((k) => k.startsWith("elb-"));
                        return {
                          value: g.name,
                          label: g.name,
                          description: isElb ? g.location : `${g.location} · no elb-* tag`,
                          disabled: !isElb,
                        };
                      });
                      items.sort((a, b) => {
                        if (a.disabled !== b.disabled) return a.disabled ? 1 : -1;
                        return a.label.localeCompare(b.label);
                      });
                      return items;
                    }
                  : null
              }
              allowCustom
              compact
              style={{ maxWidth: 220, minWidth: 140 }}
            />
            <AutoRefreshChip />
            {gettingStartedDismissed && (
              <button
                type="button"
                className="cfg-gear"
                onClick={() => {
                  sessionStorage.removeItem("elb-getting-started-dismissed");
                  setGettingStartedDismissed(false);
                  setShowGettingStarted(true);
                }}
                title="Re-open the Getting Started checklist"
                aria-label="Open Getting Started checklist"
                style={{ marginLeft: 0 }}
              >
                <HelpCircle size={14} strokeWidth={1.5} />
              </button>
            )}
            <button
              type="button"
              className="cfg-gear"
              onClick={() => setShowSettings(true)}
              title="Workspace settings"
              aria-label="Open workspace settings"
              style={{ marginLeft: 0 }}
            >
              <SettingsIcon size={14} strokeWidth={1.5} />
            </button>
          </div>
        </div>
        {/* Row 2 — description (full width, doesn't compete with controls). */}
        <div className="page-header__desc" style={{ marginTop: 0 }}>
          Live view of your BLAST workspace — clusters, registries, storage, and terminal health.
        </div>
      </header>

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
            terminalResourceGroup={config.terminalResourceGroup}
            terminalVmName={config.terminalVmName}
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
          <TerminalCard />
        </div>

        <SidecarsCard />

        <JobCard />
      </div>

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
          clusterRunning={aksQuery.data?.clusters?.some(c => c.power_state === "Running") ?? false}
          acrName={config.acrName}
          onDismiss={handleDismissGettingStarted}
        />
      )}
    </>
  );
}

/**
 * Compact dropdown for the global dashboard auto-refresh interval.
 * Styled as a `cfg-chip` so it lines up visually with the Subscription /
 * Workload RG pickers next to it.
 */
function AutoRefreshChip() {
  const { intervalMs, setIntervalMs } = useAutoRefresh();
  return (
    <label
      className="cfg-chip"
      title="How often dashboard cards refetch from Azure"
      style={{ cursor: "pointer" }}
    >
      <span className="lbl" style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
        <RefreshCw size={11} strokeWidth={1.5} />
        Auto-refresh
      </span>
      <select
        value={intervalMs}
        onChange={(e) => setIntervalMs(Number(e.target.value))}
        aria-label="Auto-refresh interval"
      >
        {AUTO_REFRESH_OPTIONS.map((opt) => (
          <option key={opt.value} value={opt.value}>
            {opt.label}
          </option>
        ))}
      </select>
    </label>
  );
}
