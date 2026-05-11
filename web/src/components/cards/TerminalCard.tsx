import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Loader2, Plus, Play, Square, Globe, HardDrive, Shield, Copy, Check, Terminal, DollarSign, RefreshCw } from "lucide-react";

import { monitoringApi, terminalApi } from "@/api/endpoints";
import { formatApiError } from "@/api/client";
import { MonitorCard } from "@/components/MonitorCard";
import { useRefreshCountdown } from "@/hooks/useRefreshCountdown";

// Approximate hourly cost by VM SKU (Korea Central, pay-as-you-go)
const SKU_COST: Record<string, number> = {
  "Standard_D2s_v5": 0.096,
  "Standard_D4s_v5": 0.192,
  "Standard_D8s_v5": 0.384,
  "Standard_D16s_v5": 0.768,
};

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

  const isRunning = query.data?.power_state === "VM running";
  const isStopped = query.data?.power_state === "VM deallocated" || query.data?.power_state === "VM stopped";

  // Health check (az login + tool versions) — only when VM is running
  const healthQuery = useQuery({
    queryKey: ["terminal-health", subscriptionId, resourceGroup, vmName],
    queryFn: () => terminalApi.health(vmName, subscriptionId, resourceGroup),
    enabled: enabled && isRunning,
    staleTime: 120_000,
    refetchInterval: 300_000,
    retry: false,
  });

  const [actionLoading, setActionLoading] = useState<"starting" | "stopping" | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const vmNotFound = query.isError && (query.error as Error).message?.toLowerCase().includes("not found");
  const otherError = query.isError && !vmNotFound;
  const isProvisioning = vmNotFound && Boolean(sessionStorage.getItem("elb-terminal-instance-id"));

  const handleStart = async () => {
    setActionLoading("starting");
    setActionError(null);
    try {
      await terminalApi.startVm(vmName, subscriptionId, resourceGroup);
      query.refetch();
    } catch (e) {
      setActionError(formatApiError(e, "terminal"));
    } finally {
      setActionLoading(null);
    }
  };

  const handleStop = async () => {
    setActionLoading("stopping");
    setActionError(null);
    try {
      await terminalApi.stopVm(vmName, subscriptionId, resourceGroup);
      query.refetch();
    } catch (e) {
      setActionError(formatApiError(e, "terminal"));
    } finally {
      setActionLoading(null);
    }
  };

  const handleCopySsh = () => {
    const host = query.data?.fqdn || query.data?.public_ip || `${vmName}.${query.data?.region}.cloudapp.azure.com`;
    navigator.clipboard.writeText(`ssh azureuser@${host}`).catch(() => {});
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

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

  const hourlyCost = query.data?.vm_size ? (SKU_COST[query.data.vm_size] ?? null) : null;

  return (
    <MonitorCard
      title="Remote Terminal"
      subtitle={enabled ? `${vmName} · ${resourceGroup}` : "Not provisioned"}
      status={status}
      fetching={query.isFetching}
      refreshCountdown={useRefreshCountdown(query.dataUpdatedAt, 30_000)}
      refreshInterval={30_000}
      onRefresh={() => { query.refetch(); if (isRunning) healthQuery.refetch(); }}
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
            <Link to="/terminal" className="glass-button" style={{ fontSize: 11 }}>
              {isProvisioning ? <><Loader2 size={12} className="spin" /> View Progress</> : <><Terminal size={12} /> Open</>}
            </Link>
          )}
        </div>
      }
    >
      {!enabled && <div className="muted">Provision a Remote Terminal to enable monitoring.</div>}
      {isProvisioning && (
        <div className="muted" style={{ color: "var(--accent)" }}>Provisioning in progress... Check the Terminal page for details.</div>
      )}
      {vmNotFound && !isProvisioning && (
        <div className="muted">VM not found — click Provision to create one.</div>
      )}
      {otherError && (
        <div className="muted" style={{ color: "var(--danger)" }}>Monitoring error: {(query.error as Error).message}</div>
      )}

      {query.data && (
        <div style={{ fontSize: 12 }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
            <tbody>
              <tr style={{ borderBottom: "1px solid var(--border-weak)" }}>
                <td style={{ padding: "5px 0", color: "var(--text-faint)", width: 90 }}>Power</td>
                <td style={{ padding: "5px 0", display: "flex", alignItems: "center", gap: 8 }}>
                  <span style={{ color: isRunning ? "var(--success)" : isStopped ? "var(--warning)" : "var(--text-muted)", fontWeight: 600 }}>
                    {isRunning ? "Running" : isStopped ? "Stopped" : query.data.power_state ?? "?"}
                  </span>
                  {isStopped && (
                    <button className="glass-button" onClick={handleStart} disabled={actionLoading !== null} style={{ fontSize: 10, padding: "2px 8px", color: "var(--success)" }}>
                      {actionLoading === "starting" ? <Loader2 size={10} className="spin" /> : <Play size={10} />} Start
                    </button>
                  )}
                  {isRunning && (
                    <button className="glass-button" onClick={handleStop} disabled={actionLoading !== null} style={{ fontSize: 10, padding: "2px 8px", color: "var(--warning)" }}>
                      {actionLoading === "stopping" ? <Loader2 size={10} className="spin" /> : <Square size={10} />} Stop
                    </button>
                  )}
                </td>
              </tr>
              <tr style={{ borderBottom: "1px solid var(--border-weak)" }}>
                <td style={{ padding: "5px 0", color: "var(--text-faint)" }}>Size</td>
                <td style={{ padding: "5px 0" }}>
                  {query.data.vm_size ?? "?"}
                  {hourlyCost !== null && (
                    <span className="muted" style={{ fontSize: 10, marginLeft: 8 }}>
                      <DollarSign size={9} style={{ verticalAlign: "middle" }} /> ~${hourlyCost.toFixed(3)}/hr
                      <span style={{ margin: "0 4px" }}>·</span>
                      ~${(hourlyCost * 24).toFixed(2)}/day
                    </span>
                  )}
                </td>
              </tr>
              {query.data.os_disk_gb && (
                <tr style={{ borderBottom: "1px solid var(--border-weak)" }}>
                  <td style={{ padding: "5px 0", color: "var(--text-faint)" }}><HardDrive size={11} style={{ verticalAlign: "middle" }} /> Disk</td>
                  <td style={{ padding: "5px 0" }}>{query.data.os_disk_gb} GB</td>
                </tr>
              )}
              <tr style={{ borderBottom: "1px solid var(--border-weak)" }}>
                <td style={{ padding: "5px 0", color: "var(--text-faint)" }}><Globe size={11} style={{ verticalAlign: "middle" }} /> IP</td>
                <td style={{ padding: "5px 0" }}>
                  {query.data.public_ip ? (
                    <code style={{ fontSize: 11 }}>{query.data.public_ip}</code>
                  ) : (
                    <span className="muted">No public IP{isStopped ? " (stopped)" : ""}</span>
                  )}
                  {query.data.fqdn && <div className="muted" style={{ fontSize: 10, marginTop: 1 }}>{query.data.fqdn}</div>}
                </td>
              </tr>
              <tr>
                <td style={{ padding: "5px 0", color: "var(--text-faint)" }}><Shield size={11} style={{ verticalAlign: "middle" }} /> Identity</td>
                <td style={{ padding: "5px 0" }}>
                  {query.data.has_managed_identity ? (
                    <span style={{ color: "var(--success)", fontSize: 11 }}>✓ {query.data.identity_type}</span>
                  ) : (
                    <span style={{ color: "var(--warning)", fontSize: 11 }}>No Managed Identity</span>
                  )}
                </td>
              </tr>
            </tbody>
          </table>

          {/* az login status */}
          {isRunning && healthQuery.data && (
            <div style={{ marginTop: 8 }}>
              <div style={{ fontSize: 10, textTransform: "uppercase", color: "var(--text-faint)", marginBottom: 4, display: "flex", alignItems: "center", gap: 4 }}>
                az login {healthQuery.isFetching && <Loader2 size={8} className="spin" />}
              </div>
              <div style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 6 }}>
                <span style={{ width: 7, height: 7, borderRadius: "50%", flexShrink: 0, background: healthQuery.data.az_login_active ? "var(--success)" : "var(--danger)" }} />
                {healthQuery.data.az_login_active ? (
                  <span>Active — <span className="muted">{healthQuery.data.az_login_user}</span></span>
                ) : (
                  <span style={{ color: "var(--warning)" }}>Expired — run <code style={{ fontSize: 10 }}>az login --use-device-code</code></span>
                )}
              </div>
            </div>
          )}

          {/* Installed tools — compact inline with status icons */}
          {isRunning && healthQuery.data && (
            <div style={{ marginTop: 8 }}>
              <div style={{ fontSize: 10, textTransform: "uppercase", color: "var(--text-faint)", marginBottom: 4 }}>Installed Tools</div>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "2px 12px", fontSize: 11 }}>
                {(["az_cli", "kubectl", "azcopy", "python"] as const).map((tool) => {
                  const version = healthQuery.data![tool];
                  const label = tool === "az_cli" ? "az CLI" : tool;
                  const ok = version && version !== "not found";
                  return [
                    <div key={`${tool}-label`} className="muted" style={{ display: "flex", alignItems: "center", gap: 3 }}>
                      <span style={{ width: 6, height: 6, borderRadius: "50%", background: ok ? "var(--success)" : "var(--danger)", flexShrink: 0 }} />
                      {label}
                    </div>,
                    <div key={`${tool}-val`}>{version || "—"}</div>,
                  ];
                })}
              </div>
            </div>
          )}

          {isRunning && healthQuery.isLoading && (
            <div className="muted" style={{ fontSize: 10, marginTop: 8, display: "flex", alignItems: "center", gap: 4 }}>
              <Loader2 size={10} className="spin" /> Checking tools & az login...
            </div>
          )}

          {/* Action buttons */}
          {isRunning && (
            <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
              {(query.data.public_ip || query.data.fqdn) && (
                <button className="glass-button" onClick={handleCopySsh} style={{ fontSize: 10, padding: "4px 10px", display: "flex", alignItems: "center", gap: 4 }}>
                  {copied ? <><Check size={10} /> Copied!</> : <><Copy size={10} /> SSH</>}
                </button>
              )}
              {healthQuery.data && (
                <button className="glass-button" onClick={() => healthQuery.refetch()} disabled={healthQuery.isFetching} style={{ fontSize: 10, padding: "4px 8px", display: "flex", alignItems: "center", gap: 4 }}>
                  <RefreshCw size={9} /> Refresh tools
                </button>
              )}
            </div>
          )}
        </div>
      )}

      {actionError && <div style={{ marginTop: 6, fontSize: 11, color: "var(--danger)" }}>{actionError}</div>}
    </MonitorCard>
  );
}
