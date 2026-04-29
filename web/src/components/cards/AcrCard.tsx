import { useQuery } from "@tanstack/react-query";

import { monitoringApi } from "@/api/endpoints";
import { MonitorCard } from "@/components/MonitorCard";

interface Props {
  subscriptionId: string;
  resourceGroup: string;
  registryName: string;
}

export function AcrCard({ subscriptionId, resourceGroup, registryName }: Props) {
  const enabled = Boolean(subscriptionId && resourceGroup && registryName);
  const query = useQuery({
    queryKey: ["acr", subscriptionId, resourceGroup, registryName],
    queryFn: () => monitoringApi.acr(subscriptionId, resourceGroup, registryName),
    enabled,
    refetchInterval: 60_000,
  });

  const status = !enabled
    ? "idle"
    : query.isLoading
      ? "loading"
      : query.isError
        ? "error"
        : "ok";

  return (
    <MonitorCard
      title="ACR"
      subtitle={enabled ? `${registryName} · ${resourceGroup}` : "Configure ACR name"}
      status={status}
    >
      {!enabled && <div className="muted">Set Subscription ID, ACR RG, and ACR Name above.</div>}
      {query.isError && <div className="muted">Failed: {(query.error as Error).message}</div>}
      {query.data && (
        <>
          <div className="muted" style={{ fontSize: 12 }}>
            {query.data.login_server} · {query.data.sku ?? "?"}
          </div>
          <table
            style={{
              width: "100%",
              marginTop: "var(--space-3)",
              borderCollapse: "collapse",
              fontSize: 13,
            }}
          >
            <thead>
              <tr style={{ textAlign: "left", color: "var(--text-muted)" }}>
                <th style={{ padding: "var(--space-2) 0" }}>Image</th>
                <th>Expected tag</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(query.data.expected_image_tags).map(([img, tag]) => (
                <tr key={img}>
                  <td style={{ padding: "var(--space-2) 0" }}>{img}</td>
                  <td>{tag}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </MonitorCard>
  );
}
