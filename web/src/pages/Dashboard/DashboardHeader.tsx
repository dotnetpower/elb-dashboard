import { HelpCircle, LayoutGrid, Settings as SettingsIcon } from "lucide-react";

import { armProxyApi } from "@/api/endpoints";
import { ResourcePicker } from "@/components/ResourcePicker";
import { saveConfig, type ResourceConfig } from "@/components/SetupWizard";
import { SubscriptionPicker } from "@/components/SubscriptionPicker";

import { AutoRefreshChip } from "./AutoRefreshChip";

export interface DashboardHeaderProps {
  config: ResourceConfig;
  setConfig: (next: ResourceConfig) => void;
  gettingStartedDismissed: boolean;
  onReopenGettingStarted: () => void;
  onOpenSettings: () => void;
}

export function DashboardHeader({
  config,
  setConfig,
  gettingStartedDismissed,
  onReopenGettingStarted,
  onOpenSettings,
}: DashboardHeaderProps) {
  return (
    <header className="dashboard-hero" aria-label="Dashboard overview">
      <div className="dashboard-hero__topline">
        <div className="dashboard-hero__title-group">
          <span className="dashboard-hero__mark" aria-hidden="true">
            <LayoutGrid size={22} strokeWidth={1.5} />
          </span>
          <div>
            <h1 className="dashboard-hero__title">ElasticBLAST Dashboard</h1>
            <p className="dashboard-hero__subtitle">
              Live workspace control for clusters, registries, storage, terminal, and
              BLAST jobs.
            </p>
          </div>
        </div>

        <div className="dashboard-hero__controls">
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
                  if (tags["elb-terminal-rg"])
                    next.terminalResourceGroup = tags["elb-terminal-rg"];
                  if (tags["elb-terminal-vm"])
                    next.terminalVmName = tags["elb-terminal-vm"];
                  if (tags["elb-region"]) next.region = tags["elb-region"];
                } catch {
                  /* RG has no elb-* tags — leave the rest of the config
                     untouched. */
                }
              }
              setConfig(next);
              saveConfig(next);
            }}
            queryKey={["arm-rgs", config.subscriptionId]}
            fetcher={
              config.subscriptionId
                ? async () => {
                    const groups = await armProxyApi.listResourceGroups(
                      config.subscriptionId,
                    );
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
              onClick={onReopenGettingStarted}
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
            onClick={onOpenSettings}
            title="Workspace settings"
            aria-label="Open workspace settings"
            style={{ marginLeft: 0 }}
          >
            <SettingsIcon size={14} strokeWidth={1.5} />
          </button>
        </div>
      </div>
    </header>
  );
}
