import { useQuery } from "@tanstack/react-query";

import { monitoringApi } from "@/api/endpoints";
import { MonitorCard } from "@/components/MonitorCard";

interface Props {
  subscriptionId: string;
  resourceGroup: string;
}

export function ClusterCard({ subscriptionId, resourceGroup }: Props) {
  const enabled = Boolean(subscriptionId && resourceGroup);
  const query = useQuery({
    queryKey: ["aks", subscriptionId, resourceGroup],
    queryFn: () => monitoringApi.aks(subscriptionId, resourceGroup),
    enabled,
    refetchInterval: 30_000,
  });

  const status = !enabled
    ? "idle"
    : query.isLoading
      ? "loading"
      : query.isError
        ? "error"
        : "ok";

  return (
    <MonitorCard title="AKS Cluster" subtitle={enabled ? resourceGroup : "Configure subscription / RG"} status={status}>
      {!enabled && <div className="muted">Set Subscription ID and Workload RG above.</div>}
      {query.isError && <div className="muted">Failed: {(query.error as Error).message}</div>}
      {query.data?.clusters.length === 0 && <div className="muted">No AKS clusters found.</div>}
      <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "grid", gap: "var(--space-3)" }}>
        {query.data?.clusters.map((c) => (
          <li key={c.name} className="glass-card" style={{ padding: "var(--space-3)" }}>
            <div style={{ display: "flex", justifyContent: "space-between" }}>
              <strong>{c.name}</strong>
              <span className="muted" style={{ fontSize: 12 }}>
                {c.region} · {c.k8s_version ?? "?"}
              </span>
            </div>
            <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
              Power: {c.power_state ?? "?"} · State: {c.provisioning_state ?? "?"} · Nodes: {c.node_count ?? "?"} {c.node_sku ? `(${c.node_sku})` : ""}
            </div>
          </li>
        ))}
      </ul>
    </MonitorCard>
  );
}
