import { HelpCircle, LayoutGrid, Settings as SettingsIcon } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";

import { armProxyApi } from "@/api/endpoints";
import { fetchResourceGroups } from "@/api/resourceGroups";
import { ResourcePicker } from "@/components/ResourcePicker";
import { saveConfig, type ResourceConfig } from "@/components/SetupWizard";
import { isAksManagedResourceGroup } from "@/lib/aksManagedRg";

import { AutoRefreshChip } from "./AutoRefreshChip";

export interface DashboardHeaderProps {
  config: ResourceConfig;
  setConfig: (next: ResourceConfig) => void;
  gettingStartedDismissed: boolean;
  onReopenGettingStarted: () => void;
  onOpenSettings: () => void;
}

/**
 * Read-only subscription caption. Sits above the interactive controls as
 * a small faint line so it doesn't compete visually with Workload RG / the
 * action buttons. The user can copy the full id but it should fade into
 * the chrome, not draw the eye.
 */
function SubscriptionLabel({ value }: { value: string }) {
  return (
    <div
      style={{
        fontSize: 11,
        color: "var(--text-faint)",
        lineHeight: 1.4,
        letterSpacing: "0.01em",
        // Right-align so the line sits visually next to the
        // controls column below.
        textAlign: "right",
        // Hard reserve 18px row height so the placeholder ("—") and the
        // populated value occupy the same vertical space; otherwise the
        // header re-flows by 2-3px on first data load.
        minHeight: 18,
      }}
    >
      Subscription ID:{" "}
      <span
        style={{
          fontFamily: "var(--font-mono, monospace)",
          color: "var(--text-muted)",
        }}
      >
        {value || "—"}
      </span>
    </div>
  );
}

export function DashboardHeader({
  config,
  setConfig,
  gettingStartedDismissed,
  onReopenGettingStarted,
  onOpenSettings,
}: DashboardHeaderProps) {
  const queryClient = useQueryClient();
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

        {/* Right column: subscription line on top (aligned with the title
            row's baseline), interactive controls below. Using a plain div
            here instead of overriding `.dashboard-hero__controls` keeps the
            mobile-grid media query for that class working unmodified. */}
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            alignItems: "flex-end",
            gap: 6,
            // Reserve enough width for the Subscription ID line so the
            // text doesn't wrap on narrow desktop widths.
            minWidth: 420,
          }}
        >
          <SubscriptionLabel value={config.subscriptionId} />
          <div className="dashboard-hero__controls">
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
                      const groups = await fetchResourceGroups(
                        queryClient,
                        config.subscriptionId,
                      );
                      const items = groups.map((g) => {
                        const tags = g.tags ?? {};
                        const isAksManaged = isAksManagedResourceGroup({
                          name: g.name,
                          tags,
                        });
                        const isElb = Object.keys(tags).some((k) => k.startsWith("elb-"));
                        let description = g.location;
                        let disabled = false;
                        if (isAksManaged) {
                          description = `${g.location} · AKS-managed (node RG)`;
                          disabled = true;
                        } else if (!isElb) {
                          description = `${g.location} · no elb-* tag`;
                          disabled = true;
                        }
                        return {
                          value: g.name,
                          label: g.name,
                          description,
                          disabled,
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
              preferKnownValue
              compact
              // Stable width so the chip does not jump from `Loading…`
              // (~80px) to `rg-elb-dashboard` (~180px) and shove the
              // neighbouring controls left mid-render. 240px comfortably
              // fits typical RG names while keeping the row right edge
              // pinned regardless of async data state.
              style={{ width: 240, minWidth: 240, maxWidth: 240 }}
            />
            <AutoRefreshChip />
            {gettingStartedDismissed && (
              <button
                type="button"
                className="cfg-gear dashboard-hide-mobile"
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
              className="cfg-gear dashboard-hide-mobile"
              onClick={onOpenSettings}
              title="Workspace settings"
              aria-label="Open workspace settings"
              style={{ marginLeft: 0 }}
            >
              <SettingsIcon size={14} strokeWidth={1.5} />
            </button>
          </div>
        </div>
      </div>
    </header>
  );
}
