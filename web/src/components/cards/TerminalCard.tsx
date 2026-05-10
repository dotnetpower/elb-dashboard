import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Loader2, Plus } from "lucide-react";

import { monitoringApi } from "@/api/endpoints";
import { MonitorCard } from "@/components/MonitorCard";
import { useRefreshCountdown } from "@/hooks/useRefreshCountdown";

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

  const vmNotFound = query.isError && (query.error as Error).message?.toLowerCase().includes("not found");
  const otherError = query.isError && !vmNotFound;
  const isProvisioning = vmNotFound && Boolean(sessionStorage.getItem("elb-terminal-instance-id"));

  const status = !enabled
    ? "idle"
    : query.isLoading
      ? "loading"
      : isProvisioning
        ? "loading"
        : vmNotFound
          ? "not-provisioned"
          : otherError
            ? "error"
            : "ok";

  return (
    <MonitorCard
      title="Remote Terminal"
      subtitle={enabled ? `${vmName} · ${resourceGroup}` : "Not provisioned"}
      status={status}
      refreshCountdown={useRefreshCountdown(query.dataUpdatedAt, 30_000)}
      refreshInterval={30_000}
      onRefresh={() => query.refetch()}
      accentColor="terminal"
      collapsible
      rightSlot={
        <div style={{ display: "flex", gap: 6 }}>
          {vmNotFound && !isProvisioning && (
            <Link to="/terminal" className="glass-button glass-button--primary" style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 4 }}>
              <Plus size={12} /> Provision
            </Link>
          )}
          {(query.data || isProvisioning) && (
            <Link to="/terminal" className="glass-button">
              {isProvisioning ? <><Loader2 size={12} className="spin" /> View Progress</> : "Open"}
            </Link>
          )}
        </div>
      }
    >
      {!enabled && <div className="muted">Provision a Remote Terminal to enable monitoring.</div>}
      {isProvisioning && (
        <div className="muted" style={{ color: "var(--accent)" }}>
          Provisioning in progress... Check the Terminal page for details.
        </div>
      )}
      {vmNotFound && !isProvisioning && (
        <div className="muted">
          VM not found yet — click Provision to create one.
        </div>
      )}
      {otherError && (
        <div className="muted" style={{ color: "var(--danger)" }}>
          Monitoring error: {(query.error as Error).message}
        </div>
      )}
      {query.data && (
        <div style={{ fontSize: 12, lineHeight: 1.6 }}>
          <div className="muted">
            Power: <strong style={{ color: query.data.power_state === "Running" ? "var(--success)" : "var(--warning)" }}>{query.data.power_state ?? "?"}</strong>
            {" · "}Size: {query.data.vm_size ?? "?"}{" · "}State: {query.data.provisioning_state ?? "?"}
          </div>
          {/* #42: Quick SSH copy — uses VM name as DNS hint */}
          {query.data.power_state === "Running" && (
            <button
              className="glass-button"
              style={{ fontSize: 10, marginTop: 6, padding: "3px 8px" }}
              onClick={() => {
                const host = `elb-term-${(query.data!.name || vmName).toLowerCase()}.${query.data!.region}.cloudapp.azure.com`;
                navigator.clipboard.writeText(`ssh azureuser@${host}`).catch(() => {});
              }}
              title="Copy SSH command to clipboard"
            >
              Copy SSH command
            </button>
          )}
        </div>
      )}
    </MonitorCard>
  );
}
