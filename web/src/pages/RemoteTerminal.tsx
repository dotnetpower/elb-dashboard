import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  CheckCircle2,
  Copy,
  Eye,
  EyeOff,
  Loader2,
  Play,
  RefreshCw,
  Server,
  Shield,
  Network,
  Key,
  Monitor,
  CloudCog,
} from "lucide-react";

import {
  type ProvisionTerminalRequest,
  terminalApi,
  monitoringApi,
} from "@/api/endpoints";
import { loadSavedConfig } from "@/components/SetupWizard";
import { SubscriptionPicker } from "@/components/SubscriptionPicker";
import { AZURE_REGIONS } from "@/constants";

const STORAGE_KEY = "elb-terminal-instance-id";

// ---------------------------------------------------------------------------
// VM Size options (common Azure sizes for bioinformatics workloads)
// ---------------------------------------------------------------------------
const VM_SIZES = [
  { value: "Standard_D2s_v5", label: "D2s v5 — 2 vCPU, 8 GB", tier: "Light" },
  { value: "Standard_D4s_v5", label: "D4s v5 — 4 vCPU, 16 GB", tier: "Recommended" },
  { value: "Standard_D8s_v5", label: "D8s v5 — 8 vCPU, 32 GB", tier: "Heavy" },
  { value: "Standard_D16s_v5", label: "D16s v5 — 16 vCPU, 64 GB", tier: "Heavy" },
  { value: "Standard_E4s_v5", label: "E4s v5 — 4 vCPU, 32 GB (memory opt)", tier: "Memory" },
  { value: "Standard_E8s_v5", label: "E8s v5 — 8 vCPU, 64 GB (memory opt)", tier: "Memory" },
] as const;

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------
const RG_RE = /^[-\w._()]+$/;
const VM_NAME_RE = /^[a-zA-Z0-9][-a-zA-Z0-9]{0,62}[a-zA-Z0-9]?$/;
const CIDR_RE = /^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\/\d{1,2}$/;

interface ValidationErrors { [key: string]: string }

function validate(form: ProvisionTerminalRequest): ValidationErrors {
  const e: ValidationErrors = {};
  if (!form.subscription_id) e.subscription_id = "Subscription is required";
  if (!form.resource_group) e.resource_group = "Resource group is required";
  else if (!RG_RE.test(form.resource_group)) e.resource_group = "Invalid resource group name";
  if (!form.region) e.region = "Region is required";
  if (!form.vm_name) e.vm_name = "VM name is required";
  else if (!VM_NAME_RE.test(form.vm_name)) e.vm_name = "1-64 chars, alphanumeric + hyphens";
  if (!form.vm_size) e.vm_size = "VM size is required";
  if (!form.admin_username) e.admin_username = "Username is required";
  else if (form.admin_username.length < 1 || form.admin_username.length > 64) e.admin_username = "1-64 characters";
  if (!form.allowed_ssh_cidr) e.allowed_ssh_cidr = "SSH CIDR is required for NSG rule";
  else if (!CIDR_RE.test(form.allowed_ssh_cidr)) e.allowed_ssh_cidr = "Must be IP/mask (e.g. 1.2.3.4/32)";
  return e;
}

// ---------------------------------------------------------------------------
// Provisioning steps for progress visualization
// ---------------------------------------------------------------------------
const PROVISION_STEPS = [
  { key: "rg", icon: Server, label: "Resource Group" },
  { key: "network", icon: Network, label: "Network & IP" },
  { key: "keyvault", icon: Key, label: "Key Vault" },
  { key: "password", icon: Shield, label: "Generate Password" },
  { key: "vm", icon: Monitor, label: "Create VM" },
  { key: "cloud-init", icon: CloudCog, label: "Cloud Init" },
] as const;

function getStepIndex(status: string | null, customStatus: unknown): number {
  if (!status) return -1;
  if (status === "Completed") return PROVISION_STEPS.length;
  if (status === "Failed" || status === "Terminated") return -2;
  const cs = customStatus as { phase?: string; step?: string } | null;
  const phase = cs?.phase ?? cs?.step;
  if (phase) {
    const idx = PROVISION_STEPS.findIndex((s) => s.key === phase);
    if (idx >= 0) return idx;
  }
  if (status === "Running" || status === "Pending") return 1;
  return 0;
}

// ---------------------------------------------------------------------------
// FieldError helper
// ---------------------------------------------------------------------------
function FieldError({ msg }: { msg?: string }) {
  if (!msg) return null;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 4, color: "var(--danger)", fontSize: 11, marginTop: 3 }}>
      <AlertTriangle size={11} /> {msg}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------
export function RemoteTerminal() {
  const [savedConfig] = useState(() => loadSavedConfig());
  const [form, setForm] = useState<ProvisionTerminalRequest>(() => ({
    subscription_id: savedConfig?.subscriptionId ?? "",
    resource_group: savedConfig?.terminalResourceGroup ?? "rg-elb-terminal",
    region: savedConfig?.region ?? "koreacentral",
    vm_name: savedConfig?.terminalVmName ?? "vm-elb-terminal",
    vm_size: "Standard_D4s_v5",
    admin_username: "azureuser",
    allowed_ssh_cidr: "",
    // Auto-populate for RBAC assignment
    workload_resource_group: savedConfig?.workloadResourceGroup ?? "",
    acr_resource_group: savedConfig?.acrResourceGroup ?? "",
    acr_name: savedConfig?.acrName ?? "",
    storage_account: savedConfig?.storageAccountName ?? "",
    storage_resource_group: savedConfig?.workloadResourceGroup ?? "",
  }));
  const [errors, setErrors] = useState<ValidationErrors>({});
  const [attempted, setAttempted] = useState(false);
  const [instanceId, setInstanceId] = useState<string | null>(() =>
    sessionStorage.getItem(STORAGE_KEY),
  );
  const [showProvisionForm, setShowProvisionForm] = useState(false);

  // Check if VM already exists via monitoring API
  const vmExistsQuery = useQuery({
    queryKey: ["terminal-exists", savedConfig?.subscriptionId, savedConfig?.terminalResourceGroup, savedConfig?.terminalVmName],
    queryFn: () => monitoringApi.terminal(
      savedConfig!.subscriptionId,
      savedConfig!.terminalResourceGroup ?? "rg-elb-terminal",
      savedConfig!.terminalVmName ?? "vm-elb-terminal",
    ),
    enabled: Boolean(savedConfig?.subscriptionId && savedConfig?.terminalResourceGroup && savedConfig?.terminalVmName) && !instanceId,
    retry: false,
    staleTime: 30_000,
  });

  const vmExists = vmExistsQuery.data && !vmExistsQuery.isError;
  const vmNotFound = vmExistsQuery.isError;

  // Auto-detect caller IP for the NSG rule.
  useEffect(() => {
    if (form.allowed_ssh_cidr) return;
    fetch("https://api.ipify.org?format=json")
      .then((r) => r.json())
      .then((j) => setForm((f) => ({ ...f, allowed_ssh_cidr: `${j.ip}/32` })))
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Live validation
  useEffect(() => {
    if (!attempted) return;
    setErrors(validate(form));
  }, [form, attempted]);

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
  const isFailed = statusQuery.data?.runtime_status === "Failed";
  const isCompleted = statusQuery.data?.runtime_status === "Completed";
  const output = statusQuery.data?.output ?? null;
  const stepIndex = getStepIndex(
    statusQuery.data?.runtime_status ?? null,
    statusQuery.data?.custom_status,
  );

  const handleStart = () => {
    setAttempted(true);
    const v = validate(form);
    setErrors(v);
    if (Object.keys(v).length > 0) return;
    startMutation.mutate(form);
  };

  const canSubmit = !startMutation.isPending && !isRunning;
  const formDisabled = isRunning || startMutation.isPending;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-5)" }}>
      <header>
        <h1 style={{ margin: 0 }}>Remote Terminal</h1>
        <p className="muted" style={{ marginTop: "var(--space-2)" }}>
          Provision a VM with elastic-blast preinstalled. The VM uses Managed
          Identity for Azure CLI and azcopy by default.
        </p>
      </header>

      {/* ── Existing VM — shown when VM already exists ── */}
      {vmExists && !instanceId && !showProvisionForm && (
        <ExistingVmCard
          vmData={vmExistsQuery.data!}
          subscriptionId={savedConfig!.subscriptionId}
          resourceGroup={savedConfig!.terminalResourceGroup ?? "rg-elb-terminal"}
          vmName={savedConfig!.terminalVmName ?? "vm-elb-terminal"}
          onRefresh={() => vmExistsQuery.refetch()}
          onReprovision={() => setShowProvisionForm(true)}
        />
      )}

      {/* ── Loading state for VM check ── */}
      {!instanceId && vmExistsQuery.isLoading && !showProvisionForm && (
        <section className="glass-card" style={{ textAlign: "center", padding: "var(--space-5)" }}>
          <Loader2 size={24} className="spin" style={{ color: "var(--accent)" }} />
          <div className="muted" style={{ marginTop: 8 }}>Checking for existing terminal VM...</div>
        </section>
      )}

      {/* ── Provisioning Form — shown when VM not found or user clicks re-provision ── */}
      {(vmNotFound || showProvisionForm || instanceId) && (
      <>
      <section className="glass-card glass-card--strong">
        <h3 style={{ marginTop: 0, display: "flex", alignItems: "center", gap: 8 }}>
          <Shield size={18} strokeWidth={1.5} /> Provisioning
        </h3>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
            gap: "var(--space-4)",
            opacity: formDisabled ? 0.6 : 1,
            pointerEvents: formDisabled ? "none" : "auto",
          }}
        >
          <div>
            <SubscriptionPicker
              value={form.subscription_id}
              onChange={(id) => setForm((f) => ({ ...f, subscription_id: id }))}
            />
            <FieldError msg={errors.subscription_id} />
          </div>

          <label>
            <span className="glass-label">Resource Group</span>
            <input className="glass-input" value={form.resource_group ?? ""} onChange={(e) => setForm({ ...form, resource_group: e.target.value })} spellCheck={false} />
            <FieldError msg={errors.resource_group} />
          </label>

          <label>
            <span className="glass-label">Region</span>
            <select className="glass-input" value={form.region ?? ""} onChange={(e) => setForm({ ...form, region: e.target.value })}>
              {AZURE_REGIONS.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
            </select>
            <FieldError msg={errors.region} />
          </label>

          <label>
            <span className="glass-label">VM Name</span>
            <input className="glass-input" value={form.vm_name ?? ""} onChange={(e) => setForm({ ...form, vm_name: e.target.value })} spellCheck={false} />
            <FieldError msg={errors.vm_name} />
          </label>

          <label>
            <span className="glass-label">VM Size</span>
            <select className="glass-input" value={form.vm_size ?? ""} onChange={(e) => setForm({ ...form, vm_size: e.target.value })}>
              {VM_SIZES.map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label} {s.tier === "Recommended" ? "★" : ""}
                </option>
              ))}
            </select>
            <FieldError msg={errors.vm_size} />
          </label>

          <label>
            <span className="glass-label">Admin Username</span>
            <input className="glass-input" value={form.admin_username ?? ""} onChange={(e) => setForm({ ...form, admin_username: e.target.value })} spellCheck={false} />
            <FieldError msg={errors.admin_username} />
          </label>

          <label>
            <span className="glass-label">Allowed SSH CIDR</span>
            <input className="glass-input" value={form.allowed_ssh_cidr ?? ""} onChange={(e) => setForm({ ...form, allowed_ssh_cidr: e.target.value })} spellCheck={false} placeholder="Auto-detected from your IP" />
            <FieldError msg={errors.allowed_ssh_cidr} />
          </label>
        </div>

        <div style={{ marginTop: "var(--space-3)", padding: "10px 14px", background: "rgba(110,159,255,0.06)", border: "1px solid rgba(110,159,255,0.15)", borderRadius: "var(--radius)", fontSize: 12, color: "var(--text-muted)", lineHeight: 1.5 }}>
          <Key size={12} style={{ display: "inline", verticalAlign: "middle", marginRight: 4 }} />
          A <strong>Key Vault</strong> will be automatically created in the same resource group to securely store the VM admin password.
        </div>

        <div style={{ display: "flex", gap: "var(--space-3)", marginTop: "var(--space-4)", alignItems: "center" }}>
          <button
            className="glass-button glass-button--primary"
            onClick={handleStart}
            disabled={!canSubmit}
            style={!canSubmit ? { opacity: 0.4, cursor: "not-allowed" } : undefined}
          >
            {startMutation.isPending ? (
              <Loader2 size={16} strokeWidth={1.5} className="spin" />
            ) : (
              <Play size={16} strokeWidth={1.5} />
            )}
            {isRunning ? "Provisioning..." : "Start Provisioning"}
          </button>
          {instanceId && (
            <button className="glass-button" onClick={() => statusQuery.refetch()} disabled={statusQuery.isFetching}>
              <RefreshCw size={14} strokeWidth={1.5} /> Refresh
            </button>
          )}
          {startMutation.isError && (
            <div style={{ display: "flex", alignItems: "center", gap: 4, color: "var(--danger)", fontSize: 12 }}>
              <AlertTriangle size={14} />
              {(startMutation.error as Error).message}
            </div>
          )}
        </div>
      </section>

      {/* ── Connection Card — shown when provisioning completed ── */}
      {instanceId && isCompleted && output && (
        <section className="glass-card">
          <ConnectionCard
            info={output}
            subscriptionId={form.subscription_id}
            resourceGroup={form.resource_group}
          />
          <div style={{ marginTop: "var(--space-3)" }}>
            <button
              className="glass-button"
              onClick={() => {
                setInstanceId(null);
                sessionStorage.removeItem(STORAGE_KEY);
                startMutation.reset();
              }}
            >
              <RefreshCw size={14} strokeWidth={1.5} /> New Provisioning
            </button>
          </div>
        </section>
      )}

      {/* ── Progress Steps — shown only while running or on failure ── */}
      {instanceId && !isCompleted && (
        <section className="glass-card">
          <h3 style={{ marginTop: 0 }}>Provisioning Progress</h3>

          <div style={{ display: "flex", flexDirection: "column", gap: 0 }}>
            {PROVISION_STEPS.map((step, i) => {
              const Icon = step.icon;
              const done = stepIndex > i;
              const active = stepIndex === i;
              const failed = stepIndex === -2 && i === 0;
              const color = done ? "var(--success)" : active ? "var(--accent)" : failed ? "var(--danger)" : "var(--text-faint)";

              return (
                <div key={step.key} style={{ display: "flex", alignItems: "center", gap: 12, padding: "10px 0" }}>
                  {/* Step indicator */}
                  <div style={{
                    width: 32, height: 32, borderRadius: "50%", display: "flex", alignItems: "center", justifyContent: "center",
                    border: `2px solid ${color}`,
                    background: done ? color : "transparent",
                    color: done ? "var(--bg-primary)" : color,
                    flexShrink: 0,
                  }}>
                    {done ? <CheckCircle2 size={16} /> : active && isRunning ? <Loader2 size={16} className="spin" /> : <Icon size={16} />}
                  </div>

                  {/* Step label */}
                  <div>
                    <div style={{ fontSize: 13, fontWeight: done || active ? 600 : 400, color: done ? "var(--success)" : active ? "var(--text-primary)" : "var(--text-faint)" }}>
                      {step.label}
                    </div>
                    {active && isRunning && step.key === "cloud-init" && (
                      <div className="muted" style={{ fontSize: 11, marginTop: 2 }}>
                        Attempt {(statusQuery.data?.custom_status as { attempt?: number })?.attempt ?? "?"} — installing tools...
                      </div>
                    )}
                  </div>

                  {/* Connector line (hidden for last) */}
                  {i < PROVISION_STEPS.length - 1 && (
                    <div style={{ flex: 1 }} />
                  )}
                </div>
              );
            })}
          </div>

          {/* Status summary */}
          <div style={{ marginTop: "var(--space-3)", padding: "10px 14px", borderRadius: "var(--radius)", fontSize: 12, lineHeight: 1.5,
            background: isFailed ? "rgba(224,123,138,0.08)" : isRunning ? "rgba(110,159,255,0.08)" : "transparent",
            border: `1px solid ${isFailed ? "rgba(224,123,138,0.2)" : isRunning ? "rgba(110,159,255,0.2)" : "var(--border-weak)"}`,
            color: isFailed ? "var(--danger)" : "var(--text-muted)",
          }}>
            {isFailed && (
              <>
                Provisioning failed.
                {statusQuery.data?.custom_status && (
                  <div style={{ marginTop: 6, padding: "8px 10px", background: "rgba(0,0,0,0.2)", borderRadius: 4, fontFamily: "var(--font-mono)", fontSize: 11, wordBreak: "break-word" }}>
                    {typeof statusQuery.data.custom_status === "string"
                      ? statusQuery.data.custom_status
                      : JSON.stringify(statusQuery.data.custom_status, null, 2)}
                  </div>
                )}
                <div style={{ marginTop: 6 }}>
                  Check your Key Vault URI, subscription, and network connectivity. You can adjust settings and retry.
                </div>
              </>
            )}
            {isRunning && "Provisioning in progress. This typically takes 5-10 minutes..."}
            {!isRunning && !isFailed && (
              <>Instance: <code>{instanceId}</code> · Status: {statusQuery.data?.runtime_status ?? "Checking..."}</>
            )}
          </div>

          {/* Reset button for failed */}
          {isFailed && (
            <div style={{ marginTop: "var(--space-3)" }}>
              <button
                className="glass-button"
                onClick={() => {
                  setInstanceId(null);
                  sessionStorage.removeItem(STORAGE_KEY);
                  startMutation.reset();
                }}
              >
                <RefreshCw size={14} strokeWidth={1.5} /> New Provisioning
              </button>
            </div>
          )}
        </section>
      )}
      </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// ExistingVmCard — shown when VM already exists (not provisioning)
// ---------------------------------------------------------------------------
import type { VmStatus } from "@/api/endpoints";

function ExistingVmCard({
  vmData,
  subscriptionId,
  resourceGroup,
  vmName,
  onRefresh,
  onReprovision,
}: {
  vmData: VmStatus;
  subscriptionId: string;
  resourceGroup: string;
  vmName: string;
  onRefresh: () => void;
  onReprovision: () => void;
}) {
  const [showPwd, setShowPwd] = useState(false);
  const [pwd, setPwd] = useState<string | null>(null);
  const [pwdLoading, setPwdLoading] = useState(false);
  const [pwdError, setPwdError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const [nsgStatus, setNsgStatus] = useState<"idle" | "loading" | "done" | "error">("idle");
  const [nsgError, setNsgError] = useState<string | null>(null);
  const [actionLoading, setActionLoading] = useState<"starting" | "stopping" | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const isRunning = vmData.power_state === "VM running";
  const isStopped = vmData.power_state === "VM deallocated" || vmData.power_state === "VM stopped";
  const host = vmData.fqdn || vmData.public_ip || `${vmName}`;
  const sshCmd = `ssh azureuser@${host}`;

  const togglePwd = async () => {
    if (showPwd) { setShowPwd(false); return; }
    if (pwd) { setShowPwd(true); return; }
    setPwdLoading(true);
    try {
      const r = await terminalApi.password(vmName, subscriptionId, resourceGroup);
      setPwd(r.password);
      setShowPwd(true);
    } catch (e) { setPwdError((e as Error).message); }
    finally { setPwdLoading(false); }
  };

  const copyText = (text: string, label: string) => {
    navigator.clipboard.writeText(text);
    setCopied(label);
    setTimeout(() => setCopied(null), 2000);
  };

  const handleOpenSsh = async () => {
    setNsgStatus("loading");
    setNsgError(null);
    try {
      const ipResp = await fetch("https://api.ipify.org?format=json").then(r => r.json());
      await terminalApi.openSsh(vmName, ipResp.ip, subscriptionId, resourceGroup);
      setNsgStatus("done");
    } catch (e) { setNsgError((e as Error).message); setNsgStatus("error"); }
  };

  const handleStartStop = async (action: "start" | "stop") => {
    setActionLoading(action === "start" ? "starting" : "stopping");
    setActionError(null);
    try {
      if (action === "start") await terminalApi.startVm(vmName, subscriptionId, resourceGroup);
      else await terminalApi.stopVm(vmName, subscriptionId, resourceGroup);
      onRefresh();
    } catch (e) { setActionError((e as Error).message); }
    finally { setActionLoading(null); }
  };

  return (
    <section className="glass-card glass-card--strong">
      <h3 style={{ marginTop: 0, display: "flex", alignItems: "center", gap: 8 }}>
        <Monitor size={18} strokeWidth={1.5} /> Remote Terminal — {vmName}
        <span style={{ fontSize: 12, fontWeight: 400, color: isRunning ? "var(--success)" : "var(--warning)", marginLeft: 8 }}>
          {isRunning ? "Running" : isStopped ? "Stopped" : vmData.power_state ?? "Unknown"}
        </span>
      </h3>

      {/* Connection info */}
      <div style={{ display: "grid", gridTemplateColumns: "120px 1fr auto", gap: "8px 12px", alignItems: "center", fontSize: 13 }}>
        <span className="muted">Host</span>
        <code style={{ overflowWrap: "anywhere" }}>{host}</code>
        <button className="glass-button" onClick={() => copyText(host, "host")} style={{ fontSize: 11 }}>
          <Copy size={12} /> {copied === "host" ? "Copied!" : "Copy"}
        </button>

        <span className="muted">Username</span>
        <code>azureuser</code>
        <button className="glass-button" onClick={() => copyText("azureuser", "user")} style={{ fontSize: 11 }}>
          <Copy size={12} /> {copied === "user" ? "Copied!" : "Copy"}
        </button>

        <span className="muted">Password</span>
        <code>{pwdLoading ? "Loading..." : showPwd ? pwd ?? "(unavailable)" : "••••••••••••"}</code>
        <div style={{ display: "flex", gap: 4 }}>
          <button className="glass-button" onClick={togglePwd} disabled={pwdLoading} style={{ fontSize: 11 }}>
            {pwdLoading ? <Loader2 size={12} className="spin" /> : showPwd ? <EyeOff size={12} /> : <Eye size={12} />}
            {showPwd ? "Hide" : "Reveal"}
          </button>
          {pwd && <button className="glass-button" onClick={() => copyText(pwd, "pwd")} style={{ fontSize: 11 }}><Copy size={12} /> {copied === "pwd" ? "Copied!" : "Copy"}</button>}
        </div>

        <span className="muted">SSH</span>
        <code style={{ overflowWrap: "anywhere" }}>{sshCmd}</code>
        <button className="glass-button" onClick={() => copyText(sshCmd, "ssh")} style={{ fontSize: 11 }}>
          <Copy size={12} /> {copied === "ssh" ? "Copied!" : "Copy"}
        </button>

        <span className="muted">VM Size</span>
        <span>{vmData.vm_size ?? "?"}</span>
        <span />

        <span className="muted">Region</span>
        <span>{vmData.region}</span>
        <span />

        {vmData.os_disk_gb && <>
          <span className="muted">Disk</span>
          <span>{vmData.os_disk_gb} GB</span>
          <span />
        </>}
      </div>

      {pwdError && <div style={{ marginTop: 8, fontSize: 12, color: "var(--danger)" }}><AlertTriangle size={12} style={{ verticalAlign: "middle" }} /> {pwdError}</div>}

      {/* Actions */}
      <div style={{ marginTop: "var(--space-4)", display: "flex", gap: "var(--space-3)", alignItems: "center", flexWrap: "wrap" }}>
        {isRunning && (
          <button className="glass-button glass-button--primary" onClick={handleOpenSsh} disabled={nsgStatus === "loading" || nsgStatus === "done"}
            style={nsgStatus === "done" ? { background: "var(--success)", borderColor: "var(--success)" } : undefined}>
            {nsgStatus === "loading" ? <Loader2 size={14} className="spin" /> : <Shield size={14} strokeWidth={1.5} />}
            {nsgStatus === "done" ? "SSH Port Opened" : "Open SSH Port (NSG)"}
          </button>
        )}
        {nsgStatus === "done" && <span style={{ fontSize: 12, color: "var(--success)" }}><CheckCircle2 size={12} style={{ verticalAlign: "middle" }} /> NSG rule added</span>}
        {nsgError && <span style={{ fontSize: 12, color: "var(--danger)" }}>{nsgError}</span>}

        {isStopped && (
          <button className="glass-button" onClick={() => handleStartStop("start")} disabled={actionLoading !== null} style={{ color: "var(--success)" }}>
            {actionLoading === "starting" ? <Loader2 size={14} className="spin" /> : <Play size={14} />} Start VM
          </button>
        )}
        {isRunning && (
          <button className="glass-button" onClick={() => handleStartStop("stop")} disabled={actionLoading !== null} style={{ color: "var(--warning)" }}>
            {actionLoading === "stopping" ? <Loader2 size={14} className="spin" /> : <Monitor size={14} />} Stop VM
          </button>
        )}

        <button className="glass-button" onClick={onRefresh} style={{ fontSize: 11 }}>
          <RefreshCw size={12} /> Refresh
        </button>
        <button className="glass-button" onClick={onReprovision} style={{ fontSize: 11, color: "var(--text-faint)" }}>
          Re-provision
        </button>
      </div>

      {actionError && <div style={{ marginTop: 8, fontSize: 12, color: "var(--danger)" }}>{actionError}</div>}

      <div className="muted" style={{ marginTop: "var(--space-4)", fontSize: 12, lineHeight: 1.6 }}>
        Azure CLI authenticates with the VM Managed Identity. If needed, run{" "}
        <code>elb-az-login-mi</code> after connecting via SSH.
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// ConnectionCard — shown after fresh provisioning completes
// ---------------------------------------------------------------------------
function ConnectionCard({
  info,
  subscriptionId,
  resourceGroup,
}: {
  info: NonNullable<Awaited<ReturnType<typeof terminalApi.status>>["output"]>;
  subscriptionId?: string;
  resourceGroup?: string;
}) {
  const [showPwd, setShowPwd] = useState(false);
  const [pwd, setPwd] = useState<string | null>(null);
  const [pwdError, setPwdError] = useState<string | null>(null);
  const [pwdLoading, setPwdLoading] = useState(false);
  const [nsgStatus, setNsgStatus] = useState<"idle" | "loading" | "done" | "error">("idle");
  const [nsgError, setNsgError] = useState<string | null>(null);
  const [stopStatus, setStopStatus] = useState<"idle" | "loading" | "done" | "error">("idle");
  const [stopError, setStopError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);

  // M4: Do NOT auto-reveal password — require explicit user click

  const togglePwd = async () => {
    if (showPwd) {
      setShowPwd(false);
      return;
    }
    if (pwd) {
      setShowPwd(true);
      return;
    }
    setPwdLoading(true);
    try {
      const r = await terminalApi.password(info.vm_name, subscriptionId, resourceGroup);
      setPwd(r.password);
      setShowPwd(true);
    } catch (e) {
      setPwdError((e as Error).message);
    } finally {
      setPwdLoading(false);
    }
  };

  const copyText = (text: string, label: string) => {
    navigator.clipboard.writeText(text);
    setCopied(label);
    setTimeout(() => setCopied(null), 2000);
  };

  const handleOpenSsh = async () => {
    setNsgStatus("loading");
    setNsgError(null);
    try {
      const ipResp = await fetch("https://api.ipify.org?format=json").then(r => r.json());
      await terminalApi.openSsh(
        info.vm_name,
        ipResp.ip,
        info.subscription_id || "",
        info.resource_group || "",
      );
      setNsgStatus("done");
    } catch (e) {
      setNsgError((e as Error).message);
      setNsgStatus("error");
    }
  };

  const handleStopVm = async () => {
    setStopStatus("loading");
    setStopError(null);
    try {
      await terminalApi.stopVm(
        info.vm_name,
        info.subscription_id || "",
        info.resource_group || "",
      );
      setStopStatus("done");
    } catch (e) {
      setStopError((e as Error).message);
      setStopStatus("error");
    }
  };

  const sshCmd = `ssh ${info.username}@${info.ssh_host}`;

  return (
    <div className="glass-card glass-card--strong" style={{ marginTop: "var(--space-4)" }}>
      <h4 style={{ marginTop: 0 }}>Connection</h4>
      <Row label="Host" value={info.ssh_host} actions={
        <button className="glass-button" onClick={() => copyText(info.ssh_host, "host")}>
          <Copy size={14} strokeWidth={1.5} /> {copied === "host" ? "Copied!" : "Copy"}
        </button>
      } />
      <Row label="Username" value={info.username} actions={
        <button className="glass-button" onClick={() => copyText(info.username, "user")}>
          <Copy size={14} strokeWidth={1.5} /> {copied === "user" ? "Copied!" : "Copy"}
        </button>
      } />
      <Row
        label="Password"
        value={pwdLoading ? "Loading..." : showPwd ? pwd ?? "(unavailable)" : "••••••••••••••••"}
        actions={
          <>
            <button className="glass-button" onClick={togglePwd} disabled={pwdLoading}>
              {pwdLoading ? <Loader2 size={14} className="spin" /> : showPwd ? <EyeOff size={14} strokeWidth={1.5} /> : <Eye size={14} strokeWidth={1.5} />}
              {showPwd ? "Hide" : "Reveal"}
            </button>
            {pwd && (
              <button className="glass-button" onClick={() => copyText(pwd, "pwd")}>
                <Copy size={14} strokeWidth={1.5} /> {copied === "pwd" ? "Copied!" : "Copy"}
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
        <button className="glass-button" onClick={() => copyText(sshCmd, "ssh")}>
          <Copy size={14} strokeWidth={1.5} /> {copied === "ssh" ? "Copied!" : "Copy"}
        </button>
      } />

      {/* NSG / SSH Access */}
      <div style={{ marginTop: "var(--space-4)", display: "flex", gap: "var(--space-3)", alignItems: "center", flexWrap: "wrap" }}>
        <button
          className="glass-button glass-button--primary"
          onClick={handleOpenSsh}
          disabled={nsgStatus === "loading" || nsgStatus === "done"}
          style={nsgStatus === "done" ? { background: "var(--success)", borderColor: "var(--success)" } : undefined}
        >
          {nsgStatus === "loading" ? <Loader2 size={14} className="spin" /> : <Shield size={14} strokeWidth={1.5} />}
          {nsgStatus === "done" ? "SSH Port Opened" : nsgStatus === "loading" ? "Opening..." : "Open SSH Port (NSG)"}
        </button>
        {nsgStatus === "done" && (
          <span style={{ fontSize: 12, color: "var(--success)" }}>
            <CheckCircle2 size={12} style={{ verticalAlign: "middle" }} /> NSG rule added for your IP
          </span>
        )}
        {nsgError && (
          <span style={{ fontSize: 12, color: "var(--danger)" }}>
            <AlertTriangle size={12} style={{ verticalAlign: "middle" }} /> {nsgError}
          </span>
        )}
        <button
          className="glass-button"
          onClick={handleStopVm}
          disabled={stopStatus === "loading" || stopStatus === "done"}
          style={stopStatus === "done" ? { opacity: 0.5 } : stopStatus === "loading" ? { opacity: 0.6 } : { color: "var(--warning)" }}
        >
          {stopStatus === "loading" ? <Loader2 size={14} className="spin" /> : <Monitor size={14} strokeWidth={1.5} />}
          {stopStatus === "done" ? "VM Stopped" : stopStatus === "loading" ? "Stopping..." : "Stop VM"}
        </button>
        {stopStatus === "done" && (
          <span style={{ fontSize: 12, color: "var(--warning)" }}>
            VM deallocated — no compute charges
          </span>
        )}
        {stopError && (
          <span style={{ fontSize: 12, color: "var(--danger)" }}>
            <AlertTriangle size={12} style={{ verticalAlign: "middle" }} /> {stopError}
          </span>
        )}
      </div>

      <div
        className="muted"
        style={{ marginTop: "var(--space-4)", fontSize: 12, lineHeight: 1.6 }}
      >
        The VM authenticates Azure CLI with Managed Identity. If the session is
        inactive, run <code>elb-az-login-mi</code>. The cloud-init script has already installed Azure CLI, kubectl,
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
