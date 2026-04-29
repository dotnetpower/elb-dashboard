import type { MonitoringConfig } from "@/pages/Dashboard";
import { SubscriptionPicker } from "@/components/SubscriptionPicker";
import { ResourcePicker } from "@/components/ResourcePicker";
import {
  listAcrs,
  listResourceGroups,
  listStorageAccounts,
  listVms,
} from "@/api/arm";

interface Props {
  config: MonitoringConfig;
  onChange: (next: MonitoringConfig) => void;
}

export function ConfigBar({ config, onChange }: Props) {
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
    <section className="glass-card">
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
          gap: "var(--space-4)",
        }}
      >
        <SubscriptionPicker
          value={config.subscriptionId}
          onChange={(id) => onChange({ ...config, subscriptionId: id })}
        />
        <ResourcePicker
          label="Workload RG"
          value={config.workloadResourceGroup}
          onChange={(v) => onChange({ ...config, workloadResourceGroup: v })}
          queryKey={["arm-rgs", sub]}
          fetcher={rgFetcher}
          allowCustom
        />
        <ResourcePicker
          label="ACR RG"
          value={config.acrResourceGroup}
          onChange={(v) => onChange({ ...config, acrResourceGroup: v })}
          queryKey={["arm-rgs", sub]}
          fetcher={rgFetcher}
          allowCustom
        />
        <ResourcePicker
          label="ACR Name"
          value={config.acrName}
          onChange={(v) => onChange({ ...config, acrName: v })}
          queryKey={["arm-acrs", sub, config.acrResourceGroup]}
          fetcher={
            sub && config.acrResourceGroup
              ? async () =>
                  (await listAcrs(sub, config.acrResourceGroup)).map((r) => ({
                    value: r.name,
                    label: r.name,
                    description: r.loginServer ?? r.location,
                  }))
              : null
          }
          disabledPlaceholder="Pick ACR RG first"
        />
        <ResourcePicker
          label="Storage Account"
          value={config.storageAccountName}
          onChange={(v) => onChange({ ...config, storageAccountName: v })}
          queryKey={["arm-storage", sub, config.workloadResourceGroup]}
          fetcher={
            sub && config.workloadResourceGroup
              ? async () =>
                  (await listStorageAccounts(sub, config.workloadResourceGroup)).map(
                    (s) => ({
                      value: s.name,
                      label: s.name,
                      description: s.location,
                    }),
                  )
              : null
          }
          disabledPlaceholder="Pick Workload RG first"
        />
        <ResourcePicker
          label="Terminal RG"
          value={config.terminalResourceGroup}
          onChange={(v) => onChange({ ...config, terminalResourceGroup: v })}
          queryKey={["arm-rgs", sub]}
          fetcher={rgFetcher}
          allowCustom
        />
        <ResourcePicker
          label="Terminal VM"
          value={config.terminalVmName}
          onChange={(v) => onChange({ ...config, terminalVmName: v })}
          queryKey={["arm-vms", sub, config.terminalResourceGroup]}
          fetcher={
            sub && config.terminalResourceGroup
              ? async () =>
                  (await listVms(sub, config.terminalResourceGroup)).map((v) => ({
                    value: v.name,
                    label: v.name,
                    description: v.location,
                  }))
              : null
          }
          disabledPlaceholder="Pick Terminal RG first"
        />
      </div>
    </section>
  );
}
