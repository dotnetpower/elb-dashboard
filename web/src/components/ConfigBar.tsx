import { useState } from "react";
import { Settings, ChevronDown, ChevronUp } from "lucide-react";

import type { MonitoringConfig } from "@/pages/Dashboard";
import { SubscriptionPicker } from "@/components/SubscriptionPicker";
import { ResourcePicker } from "@/components/ResourcePicker";
import { armProxyApi } from "@/api/endpoints";
import { isAksManagedResourceGroup } from "@/lib/aksManagedRg";

interface Props {
  config: MonitoringConfig;
  onChange: (next: MonitoringConfig) => void;
  onOpenSettings?: () => void;
}

export function ConfigBar({ config, onChange, onOpenSettings }: Props) {
  const [expanded, setExpanded] = useState(true);
  const sub = config.subscriptionId;
  const rgFetcher = sub
    ? async () => {
        const groups = await armProxyApi.listResourceGroups(sub);
        // Hide Azure-managed infrastructure RGs (`MC_…`, `ME_…`) entirely;
        // they may inherit `elb-*` tags but are never dashboard workspaces.
        // RGs without any `elb-*` tag remain visible but disabled so users
        // can see they exist without accidentally selecting them.
        const items = groups
          .filter(
            (g) =>
              !isAksManagedResourceGroup({
                name: g.name,
                tags: g.tags ?? {},
              }),
          )
          .map((g) => {
            const tags = g.tags ?? {};
            const isElb = Object.keys(tags).some((k) => k.startsWith("elb-"));
            let description = g.location;
            let disabled = false;
            if (!isElb) {
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
        // Selectable (elb-tagged) RGs first, then disabled rows; keep
        // alpha order within each bucket.
        items.sort((a, b) => {
          if (a.disabled !== b.disabled) return a.disabled ? 1 : -1;
          return a.label.localeCompare(b.label);
        });
        return items;
      }
    : null;

  return (
    <div className="config-strip">
      {/* #18 Collapse toggle */}
      <button
        className="cfg-gear"
        onClick={() => setExpanded((p) => !p)}
        title={expanded ? "Collapse config" : "Expand config"}
        style={{ marginLeft: 0, marginRight: 4 }}
      >
        {expanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
      </button>

      {expanded ? (
        <>
          {/* Primary selects */}
          <SubscriptionPicker
            value={config.subscriptionId}
            onChange={(id) => onChange({ ...config, subscriptionId: id })}
            compact
            style={{ flex: 2, minWidth: 0 }}
          />
          <div className="cfg-sep" />
          <ResourcePicker
            label="Workload RG"
            value={config.workloadResourceGroup}
            onChange={async (v) => {
              const next = { ...config, workloadResourceGroup: v };
              // Auto-load associated resources from RG tags
              if (sub && v) {
                try {
                  const { tags } = await armProxyApi.getRgTags(sub, v);
                  if (tags["elb-acr-rg"]) next.acrResourceGroup = tags["elb-acr-rg"];
                  if (tags["elb-acr"]) next.acrName = tags["elb-acr"];
                  if (tags["elb-storage"]) next.storageAccountName = tags["elb-storage"];
                  if (tags["elb-terminal-rg"])
                    next.terminalResourceGroup = tags["elb-terminal-rg"];
                  if (tags["elb-terminal-vm"])
                    next.terminalVmName = tags["elb-terminal-vm"];
                  if (tags["elb-region"]) next.region = tags["elb-region"];
                } catch {
                  /* tags not found — keep existing config */
                }
              }
              onChange(next);
            }}
            queryKey={["arm-rgs", sub]}
            fetcher={rgFetcher}
            allowCustom
            compact
            style={{ flex: 1, minWidth: 0 }}
          />
        </>
      ) : (
        <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
          {config.subscriptionId
            ? config.subscriptionId.slice(0, 8) + "…"
            : "No subscription"}{" "}
          · {config.workloadResourceGroup || "—"}
        </span>
      )}

      {/* Settings gear */}
      {onOpenSettings && (
        <button
          className="cfg-gear"
          onClick={onOpenSettings}
          title="Resource Settings"
          aria-label="Open resource settings"
        >
          <Settings size={14} strokeWidth={1.5} />
        </button>
      )}
    </div>
  );
}
