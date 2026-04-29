import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Copy, Eye, EyeOff, Loader2, Play, RefreshCw } from "lucide-react";

import {
  type ProvisionTerminalRequest,
  terminalApi,
} from "@/api/endpoints";
import { SubscriptionPicker } from "@/components/SubscriptionPicker";

const STORAGE_KEY = "elb-terminal-instance-id";

export function RemoteTerminal() {
  const [form, setForm] = useState<ProvisionTerminalRequest>({
    subscription_id: "",
    resource_group: "rg-elb-terminal",
    region: "koreacentral",
    vm_name: "vm-elb-terminal",
    vm_size: "Standard_D4s_v5",
    admin_username: "azureuser",
    allowed_ssh_cidr: "",
  });
  const [instanceId, setInstanceId] = useState<string | null>(() =>
    sessionStorage.getItem(STORAGE_KEY),
  );

  // Auto-detect caller IP for the NSG rule.
  useEffect(() => {
    if (form.allowed_ssh_cidr) return;
    fetch("https://api.ipify.org?format=json")
      .then((r) => r.json())
      .then((j) => setForm((f) => ({ ...f, allowed_ssh_cidr: `${j.ip}/32` })))
      .catch(() => {
        // ignore — user can fill manually
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const startMutation = useMutation({
    mutationFn: (payload: ProvisionTerminalRequest) =>
      terminalApi.provision(payload),
    onSuccess: (resp) => {
      setInstanceId(resp.id);
      sessionStorage.setItem(STORAGE_KEY, resp.id);
    },
  });

  const statusQuery = useQuery({
    queryKey: ["terminal-status", instanceId],
    queryFn: () => terminalApi.status(instanceId!),
    enabled: Boolean(instanceId),
    refetchInterval: (q) => {
      const data = q.state.data;
      if (!data) return 3_000;
      return data.runtime_status === "Completed" ||
        data.runtime_status === "Failed" ||
        data.runtime_status === "Terminated"
        ? false
        : 5_000;
    },
  });

  const isRunning =
    statusQuery.data?.runtime_status &&
    !["Completed", "Failed", "Terminated"].includes(statusQuery.data.runtime_status);
  const output = statusQuery.data?.output ?? null;
  const phase = useMemo(() => {
    if (!instanceId) return "Not started";
    if (!statusQuery.data) return "Loading…";
    if (output) return "Ready";
    return statusQuery.data.runtime_status;
  }, [instanceId, statusQuery.data, output]);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-5)" }}>
      <header>
        <h1 style={{ margin: 0 }}>Remote Terminal</h1>
        <p className="muted" style={{ marginTop: "var(--space-2)" }}>
          Provision a VM with elastic-blast preinstalled. After it boots, run{" "}
          <code>az login --use-device-code</code> over SSH and you are ready.
        </p>
      </header>

      <section className="glass-card glass-card--strong">
        <h3 style={{ marginTop: 0 }}>Provisioning</h3>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
            gap: "var(--space-4)",
          }}
        >
          <SubscriptionPicker
            value={form.subscription_id}
            onChange={(id) => setForm((f) => ({ ...f, subscription_id: id }))}
          />
          {(
            [
              ["resource_group", "Resource Group"],
              ["region", "Region"],
              ["vm_name", "VM Name"],
              ["vm_size", "VM Size"],
              ["admin_username", "Admin Username"],
              ["allowed_ssh_cidr", "Allowed SSH CIDR"],
            ] as const
          ).map(([key, label]) => (
            <label key={key}>
              <span className="glass-label">{label}</span>
              <input
                className="glass-input"
                value={String(form[key] ?? "")}
                onChange={(e) => setForm({ ...form, [key]: e.target.value })}
                spellCheck={false}
              />
            </label>
          ))}
        </div>

        <div
          style={{
            display: "flex",
            gap: "var(--space-3)",
            marginTop: "var(--space-4)",
            alignItems: "center",
          }}
        >
          <button
            className="glass-button glass-button--primary"
            onClick={() => startMutation.mutate(form)}
            disabled={startMutation.isPending || isRunning || !form.subscription_id}
          >
            {startMutation.isPending ? (
              <Loader2 size={16} strokeWidth={1.5} className="spin" />
            ) : (
              <Play size={16} strokeWidth={1.5} />
            )}
            Start provisioning
          </button>
          {instanceId && (
            <button
              className="glass-button"
              onClick={() => statusQuery.refetch()}
              disabled={statusQuery.isFetching}
            >
              <RefreshCw size={14} strokeWidth={1.5} /> Refresh
            </button>
          )}
          {startMutation.isError && (
            <span className="muted" style={{ color: "var(--danger)" }}>
              {(startMutation.error as Error).message}
            </span>
          )}
        </div>
      </section>

      {instanceId && (
        <section className="glass-card">
          <h3 style={{ marginTop: 0 }}>Status</h3>
          <div className="muted" style={{ fontSize: 12, marginBottom: "var(--space-3)" }}>
            Instance: <code>{instanceId}</code> · Phase: <strong>{phase}</strong>
          </div>
          {Boolean(statusQuery.data?.custom_status) && (
            <pre
              className="glass-card"
              style={{ padding: "var(--space-3)", fontSize: 12, overflow: "auto" }}
            >
              {JSON.stringify(statusQuery.data?.custom_status, null, 2)}
            </pre>
          )}

          {output && <ConnectionCard info={output} />}
        </section>
      )}
    </div>
  );
}

function ConnectionCard({
  info,
}: {
  info: NonNullable<Awaited<ReturnType<typeof terminalApi.status>>["output"]>;
}) {
  const [showPwd, setShowPwd] = useState(false);
  const [pwd, setPwd] = useState<string | null>(null);
  const [pwdError, setPwdError] = useState<string | null>(null);

  const reveal = async () => {
    if (pwd) {
      setShowPwd(true);
      return;
    }
    try {
      const r = await terminalApi.password(info.vm_name);
      setPwd(r.password);
      setShowPwd(true);
    } catch (e) {
      setPwdError((e as Error).message);
    }
  };

  const sshCmd = `ssh ${info.username}@${info.ssh_host}`;

  return (
    <div className="glass-card glass-card--strong" style={{ marginTop: "var(--space-4)" }}>
      <h4 style={{ marginTop: 0 }}>Connection</h4>
      <Row label="Host" value={info.ssh_host} />
      <Row label="Username" value={info.username} />
      <Row
        label="Password"
        value={showPwd ? pwd ?? "(unavailable)" : "••••••••••••••••"}
        actions={
          <>
            <button className="glass-button" onClick={reveal}>
              {showPwd ? <EyeOff size={14} strokeWidth={1.5} /> : <Eye size={14} strokeWidth={1.5} />}
              {showPwd ? "Hide" : "Reveal"}
            </button>
            {pwd && (
              <button
                className="glass-button"
                onClick={() => navigator.clipboard.writeText(pwd)}
              >
                <Copy size={14} strokeWidth={1.5} /> Copy
              </button>
            )}
          </>
        }
      />
      {pwdError && (
        <div className="muted" style={{ color: "var(--danger)", fontSize: 12 }}>
          {pwdError}
        </div>
      )}
      <Row label="ssh" value={sshCmd} actions={
        <button className="glass-button" onClick={() => navigator.clipboard.writeText(sshCmd)}>
          <Copy size={14} strokeWidth={1.5} /> Copy
        </button>
      } />
      <div
        className="muted"
        style={{ marginTop: "var(--space-4)", fontSize: 12, lineHeight: 1.6 }}
      >
        Once connected, run <code>az login --use-device-code</code> to authenticate
        the VM. The cloud-init script has already installed Azure CLI, kubectl,
        azcopy, Python 3.11 + venv, and cloned <code>elastic-blast-azure</code>.
        cloud-init status: <strong>{info.cloud_init_status}</strong>.
      </div>
    </div>
  );
}

function Row({
  label,
  value,
  actions,
}: {
  label: string;
  value: string;
  actions?: ReactNode;
}) {
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "120px 1fr auto",
        gap: "var(--space-3)",
        alignItems: "center",
        padding: "var(--space-2) 0",
        borderBottom: "1px solid var(--glass-border)",
      }}
    >
      <span className="muted" style={{ fontSize: 12 }}>
        {label}
      </span>
      <code style={{ overflowWrap: "anywhere" }}>{value}</code>
      <div style={{ display: "flex", gap: "var(--space-2)" }}>{actions}</div>
    </div>
  );
}
