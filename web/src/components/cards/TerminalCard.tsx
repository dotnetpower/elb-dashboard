import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { monitoringApi } from "@/api/endpoints";
import { MonitorCard } from "@/components/MonitorCard";

interface Props {
  subscriptionId: string;
  resourceGroup: string;
  vmName: string;
}

export function TerminalCard({ subscriptionId, resourceGroup, vmName }: Props) {
  const enabled = Boolean(subscriptionId && resourceGroup && vmName);
  const query = useQuery({
    queryKey: ["terminal", subscriptionId, resourceGroup, vmName],
    queryFn: () => monitoringApi.terminal(subscriptionId, resourceGroup, vmName),
    enabled,
    refetchInterval: 30_000,
    retry: false,
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
      title="Remote Terminal"
      subtitle={enabled ? `${vmName} · ${resourceGroup}` : "Not provisioned"}
      status={status}
      rightSlot={
        <Link to="/terminal" className="glass-button">
          Open
        </Link>
      }
    >
      {!enabled && <div className="muted">Provision a Remote Terminal to enable monitoring.</div>}
      {query.isError && (
        <div className="muted">
          VM not found yet — provision one from the Remote Terminal page.
        </div>
      )}
      {query.data && (
        <div className="muted" style={{ fontSize: 12, lineHeight: 1.6 }}>
          Power: <strong>{query.data.power_state ?? "?"}</strong>
          <br />
          Size: {query.data.vm_size ?? "?"}
          <br />
          State: {query.data.provisioning_state ?? "?"}
        </div>
      )}
    </MonitorCard>
  );
}
