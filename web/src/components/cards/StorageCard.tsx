import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/api/client";
import { monitoringApi } from "@/api/endpoints";
import { MonitorCard } from "@/components/MonitorCard";

interface Props {
  subscriptionId: string;
  resourceGroup: string;
  accountName: string;
}

export function StorageCard({ subscriptionId, resourceGroup, accountName }: Props) {
  const enabled = Boolean(subscriptionId && resourceGroup && accountName);
  const queryClient = useQueryClient();
  const queryKey = ["storage", subscriptionId, resourceGroup, accountName];

  const query = useQuery({
    queryKey,
    queryFn: () => monitoringApi.storage(subscriptionId, resourceGroup, accountName),
    enabled,
    refetchInterval: 30_000,
  });

  const toggle = useMutation({
    mutationFn: (next: boolean) =>
      api.post<{ public_network_access: string | null }>(
        "/monitor/storage/public-access",
        {
          subscription_id: subscriptionId,
          resource_group: resourceGroup,
          account_name: accountName,
          enabled: next,
        },
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey });
    },
  });

  const status = !enabled
    ? "idle"
    : query.isLoading
      ? "loading"
      : query.isError
        ? "error"
        : query.data?.public_network_access === "Enabled"
          ? "loading"
          : "ok";
  const publicAccess = query.data?.public_network_access ?? "?";
  const isPublic = publicAccess === "Enabled";

  return (
    <MonitorCard
      title="Storage Account"
      subtitle={enabled ? `${accountName} · ${resourceGroup}` : "Configure account name"}
      status={status}
      rightSlot={
        enabled && (
          <button
            className={isPublic ? "glass-button" : "glass-button glass-button--primary"}
            onClick={() => toggle.mutate(!isPublic)}
            disabled={toggle.isPending}
          >
            {isPublic ? "Disable public access" : "Enable public access"}
          </button>
        )
      }
    >
      {!enabled && <div className="muted">Set Subscription ID, Workload RG, and Storage Account above.</div>}
      {query.isError && <div className="muted">Failed: {(query.error as Error).message}</div>}
      {query.data && (
        <>
          <div className="muted" style={{ fontSize: 12 }}>
            {query.data.region} · {query.data.sku} · HNS: {String(query.data.is_hns_enabled)} · Public: <strong style={{ color: isPublic ? "var(--warning)" : "var(--success)" }}>{publicAccess}</strong>
          </div>
          <ul style={{ marginTop: "var(--space-3)", padding: 0, listStyle: "none", display: "grid", gap: "var(--space-2)" }}>
            {query.data.containers.map((c) => (
              <li key={c.name} className="glass-card" style={{ padding: "var(--space-3)" }}>
                <strong>{c.name}</strong>
                <span className="muted" style={{ fontSize: 12, marginLeft: "var(--space-2)" }}>
                  {c.public_access ?? "private"}
                </span>
              </li>
            ))}
          </ul>
        </>
      )}
    </MonitorCard>
  );
}
