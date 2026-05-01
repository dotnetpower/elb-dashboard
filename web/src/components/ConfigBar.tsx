import { Settings } from "lucide-react";

import type { MonitoringConfig } from "@/pages/Dashboard";
import { SubscriptionPicker } from "@/components/SubscriptionPicker";
import { ResourcePicker } from "@/components/ResourcePicker";
import { listResourceGroups } from "@/api/arm";

interface Props {
  config: MonitoringConfig;
  onChange: (next: MonitoringConfig) => void;
  onOpenSettings?: () => void;
}

export function ConfigBar({ config, onChange, onOpenSettings }: Props) {
  const sub = config.subscriptionId;
  const rgFetcher = sub
    ? async () =>
        (await listResourceGroups(sub)).map((g) => ({
          value: g.name,
          label: g.name,
          description: g.location,
        }))
    : null;

  return (
    <div className="config-strip">
      {/* Primary selects */}
      <SubscriptionPicker
        value={config.subscriptionId}
        onChange={(id) => onChange({ ...config, subscriptionId: id })}
        compact
      />
      <div className="cfg-sep" />
      <ResourcePicker
        label="Workload RG"
        value={config.workloadResourceGroup}
        onChange={(v) => onChange({ ...config, workloadResourceGroup: v })}
        queryKey={["arm-rgs", sub]}
        fetcher={rgFetcher}
        allowCustom
        compact
      />

      {/* Read-only summary pills */}
      <div className="env-pills">
        {config.acrName && (
          <div className="env-pill">
            <div className="pill-dot" /> {config.acrName}
          </div>
        )}
        {config.storageAccountName && (
          <div className="env-pill">
            <div className="pill-dot" /> {config.storageAccountName}
          </div>
        )}
        {config.terminalVmName && (
          <div className="env-pill">
            <div className="pill-dot" /> {config.terminalVmName}
          </div>
        )}
      </div>

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
