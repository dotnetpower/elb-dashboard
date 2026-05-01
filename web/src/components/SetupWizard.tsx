import { useCallback, useEffect, useState, type ReactNode } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
import { Loader2, AlertTriangle, CheckCircle2, Plus } from "lucide-react";

import { armProxyApi, resourceApi } from "@/api/endpoints";
import { Tooltip } from "@/components/Tooltip";

const STORAGE_KEY = "elb-resource-config";

export interface ResourceConfig {
  subscriptionId: string;
  workloadResourceGroup: string;
  acrResourceGroup: string;
  acrName: string;
  storageAccountName: string;
  terminalResourceGroup: string;
  terminalVmName: string;
  region: string;
}

const DEFAULTS: ResourceConfig = {
  subscriptionId: "",
  workloadResourceGroup: "",
  acrResourceGroup: "",
  acrName: "",
  storageAccountName: "",
  terminalResourceGroup: "rg-elb-terminal",
  terminalVmName: "vm-elb-terminal",
  region: "koreacentral",
};

export function loadSavedConfig(): ResourceConfig | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as ResourceConfig;
    if (!parsed.subscriptionId || !parsed.workloadResourceGroup) return null;
    return parsed;
  } catch {
    return null;
  }
}

export function saveConfig(config: ResourceConfig): void {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(config));
}

export function clearConfig(): void {
  localStorage.removeItem(STORAGE_KEY);
}

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const RG_RE = /^[-\w._()]+$/;
const STORAGE_RE = /^[a-z0-9]{3,24}$/;
const ACR_RE = /^[a-zA-Z0-9]{5,50}$/;
const VM_RE = /^[a-zA-Z0-9][-a-zA-Z0-9]{0,62}[a-zA-Z0-9]?$/;

interface ValidationErrors { [key: string]: string }

function validateStep1(c: ResourceConfig): ValidationErrors {
  const e: ValidationErrors = {};
  if (!c.subscriptionId) e.subscriptionId = "Subscription ID is required";
  else if (!UUID_RE.test(c.subscriptionId)) e.subscriptionId = "Must be a valid UUID (e.g. 12345678-1234-1234-1234-123456789abc)";
  return e;
}

function validateStep2(c: ResourceConfig): ValidationErrors {
  const e: ValidationErrors = {};
  if (!c.workloadResourceGroup) e.workloadResourceGroup = "Workload RG is required";
  else if (!RG_RE.test(c.workloadResourceGroup)) e.workloadResourceGroup = "Invalid name. Use letters, numbers, hyphens, underscores.";
  if (!c.acrResourceGroup) e.acrResourceGroup = "ACR RG is required";
  else if (!RG_RE.test(c.acrResourceGroup)) e.acrResourceGroup = "Invalid name";
  if (!c.terminalResourceGroup) e.terminalResourceGroup = "Terminal RG is required";
  else if (!RG_RE.test(c.terminalResourceGroup)) e.terminalResourceGroup = "Invalid name";
  if (!c.region) e.region = "Region is required";
  return e;
}

function validateStep3(c: ResourceConfig): ValidationErrors {
  const e: ValidationErrors = {};
  if (c.storageAccountName && !STORAGE_RE.test(c.storageAccountName)) e.storageAccountName = "3-24 lowercase letters and numbers only";
  if (c.acrName && !ACR_RE.test(c.acrName)) e.acrName = "5-50 alphanumeric characters only";
  if (c.terminalVmName && !VM_RE.test(c.terminalVmName)) e.terminalVmName = "Invalid VM name";
  return e;
}

const REGIONS = [
  "koreacentral", "koreasouth", "eastus", "eastus2", "westus", "westus2",
  "centralus", "northeurope", "westeurope", "southeastasia", "eastasia",
  "japaneast", "japanwest", "australiaeast",
];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function ErrorMsg({ msg }: { msg?: string }) {
  if (!msg) return null;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 4, color: "var(--danger)", fontSize: 11, marginTop: 4 }}>
      <AlertTriangle size={11} /> {msg}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------
interface Props { onComplete: (config: ResourceConfig) => void }
type Step = 1 | 2 | 3 | 4;

export function SetupWizard({ onComplete }: Props) {
  const [step, setStep] = useState<Step>(1);
  const [config, setConfig] = useState<ResourceConfig>(DEFAULTS);
  const [errors, setErrors] = useState<ValidationErrors>({});
  const [attempted, setAttempted] = useState(false);

  // ── Step 1: Subscriptions (via backend → az login) ──
  const subsQuery = useQuery({ queryKey: ["wizard-subs"], queryFn: armProxyApi.listSubscriptions, staleTime: 5 * 60_000, retry: 1 });
  useEffect(() => {
    if (!config.subscriptionId && subsQuery.data?.length)
      setConfig((c) => ({ ...c, subscriptionId: subsQuery.data[0].subscriptionId }));
  }, [config.subscriptionId, subsQuery.data]);

  // ── Step 2: Resource groups (via backend → az login) ──
  const rgQuery = useQuery({
    queryKey: ["wizard-rgs", config.subscriptionId],
    queryFn: () => armProxyApi.listResourceGroups(config.subscriptionId),
    enabled: Boolean(config.subscriptionId) && UUID_RE.test(config.subscriptionId),
    staleTime: 30_000, retry: 1,
  });

  // ── Step 3: Discovery (via backend → az login) ──
  const storageQuery = useQuery({
    queryKey: ["wizard-storage", config.subscriptionId, config.workloadResourceGroup],
    queryFn: () => armProxyApi.listStorageAccounts(config.subscriptionId, config.workloadResourceGroup),
    enabled: step >= 3 && Boolean(config.subscriptionId && config.workloadResourceGroup), retry: 1,
  });
  const acrQuery = useQuery({
    queryKey: ["wizard-acr", config.subscriptionId, config.acrResourceGroup],
    queryFn: () => armProxyApi.listAcrs(config.subscriptionId, config.acrResourceGroup),
    enabled: step >= 3 && Boolean(config.subscriptionId && config.acrResourceGroup), retry: 1,
  });
  const vmQuery = useQuery({
    queryKey: ["wizard-vm", config.subscriptionId, config.terminalResourceGroup],
    queryFn: () => armProxyApi.listVms(config.subscriptionId, config.terminalResourceGroup),
    enabled: step >= 3 && Boolean(config.subscriptionId && config.terminalResourceGroup), retry: 1,
  });

  // Auto-fill discovered resources
  useEffect(() => {
    if (step !== 3) return;
    setConfig((c) => {
      const n = { ...c };
      if (!n.storageAccountName && storageQuery.data?.length) n.storageAccountName = storageQuery.data[0].name;
      if (!n.acrName && acrQuery.data?.length) n.acrName = acrQuery.data[0].name;
      if (!n.terminalVmName && vmQuery.data?.length) n.terminalVmName = vmQuery.data[0].name;
      return n;
    });
  }, [step, storageQuery.data, acrQuery.data, vmQuery.data]);

  // ── Mutations for resource creation ──
  const createStorageMut = useMutation({
    mutationFn: () => resourceApi.ensureStorage({
      subscription_id: config.subscriptionId, resource_group: config.workloadResourceGroup,
      account_name: config.storageAccountName, region: config.region,
    }),
  });
  const createAcrMut = useMutation({
    mutationFn: () => resourceApi.ensureAcr({
      subscription_id: config.subscriptionId, resource_group: config.acrResourceGroup,
      registry_name: config.acrName, region: config.region,
    }),
  });

  // ── Navigation ──
  const handleNext = useCallback(() => {
    setAttempted(true);
    const v = step === 1 ? validateStep1(config) : step === 2 ? validateStep2(config) : step === 3 ? validateStep3(config) : {};
    setErrors(v);
    if (Object.keys(v).length > 0) return;
    setAttempted(false); setErrors({});
    setStep((s) => (s + 1) as Step);
  }, [step, config]);

  const handleFinish = useCallback(() => { saveConfig(config); onComplete(config); }, [config, onComplete]);

  // Live validation
  useEffect(() => {
    if (!attempted) return;
    const v = step === 1 ? validateStep1(config) : step === 2 ? validateStep2(config) : step === 3 ? validateStep3(config) : {};
    setErrors(v);
  }, [config, step, attempted]);

  const stepLabels = ["Subscription", "Resource Groups", "Discover", "Confirm"];

  // ── Render ──
  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 100, backdropFilter: "blur(4px)" }}>
      <div style={{ background: "var(--bg-primary)", border: "1px solid var(--border-medium)", borderRadius: 16, boxShadow: "0 8px 48px rgba(0,0,0,0.5)", width: 720, maxHeight: "90vh", overflow: "hidden", display: "flex", flexDirection: "column" }}>

        {/* Header */}
        <div style={{ padding: "24px 32px 0", display: "flex", alignItems: "center", gap: 12 }}>
          <div style={{ width: 36, height: 36, borderRadius: 10, background: "linear-gradient(135deg, #6e9fff, #b877d9)", boxShadow: "0 2px 12px rgba(110,159,255,0.25)" }} />
          <div>
            <h1 style={{ fontSize: 18, fontWeight: 700, margin: 0 }}>ElasticBLAST Setup</h1>
            <div style={{ fontSize: 12, color: "var(--text-faint)", marginTop: 1 }}>Configure your Azure resources</div>
          </div>
        </div>

        {/* Stepper */}
        <div style={{ padding: "20px 32px", display: "flex", alignItems: "center" }}>
          {stepLabels.map((label, i) => {
            const n = (i + 1) as Step, done = n < step, active = n === step;
            return (<div key={n} style={{ display: "contents" }}>
              {i > 0 && <div style={{ flex: 1, height: 2, margin: "0 12px", minWidth: 24, background: done ? "var(--success)" : "var(--border-weak)" }} />}
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <div style={{ width: 28, height: 28, borderRadius: "50%", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 12, fontWeight: 700, flexShrink: 0, border: `2px solid ${done ? "var(--success)" : active ? "var(--accent)" : "var(--border-medium)"}`, color: done ? "var(--bg-primary)" : active ? "var(--accent)" : "var(--text-faint)", background: done ? "var(--success)" : active ? "rgba(110,159,255,0.08)" : "transparent" }}>
                  {done ? "✓" : n}
                </div>
                <span style={{ fontSize: 12, whiteSpace: "nowrap", color: done ? "var(--success)" : active ? "var(--text-primary)" : "var(--text-faint)", fontWeight: active ? 500 : 400 }}>{label}</span>
              </div>
            </div>);
          })}
        </div>

        {/* Body */}
        <div style={{ padding: "8px 32px 24px", flex: 1, overflowY: "auto" }}>

          {/* Step 1 */}
          {step === 1 && (<div>
            <h2 style={{ fontSize: 14, fontWeight: 600, marginBottom: 4 }}>Choose Subscription</h2>
            <p style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 14, lineHeight: 1.5 }}>Select the Azure subscription where your ElasticBLAST resources live.</p>
            <label style={{ display: "block" }}>
              <span className="glass-label">Subscription</span>
              {subsQuery.isLoading ? (
                <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "10px 0", color: "var(--text-muted)" }}><Loader2 size={14} className="spin" /> Loading subscriptions...</div>
              ) : subsQuery.isError ? (<>
                <div style={{ color: "var(--warning)", fontSize: 12, marginBottom: 8, lineHeight: 1.5 }}>Could not load subscriptions. Enter your Subscription ID manually:</div>
                <input className="glass-input" placeholder="12345678-1234-1234-1234-123456789abc" value={config.subscriptionId} onChange={(e) => setConfig((c) => ({ ...c, subscriptionId: e.target.value.trim() }))} spellCheck={false} style={{ fontFamily: "var(--font-mono)", fontSize: 12 }} />
              </>) : (
                <select className="glass-input" value={config.subscriptionId} onChange={(e) => setConfig((c) => ({ ...c, subscriptionId: e.target.value }))}>
                  <option value="">Select a subscription</option>
                  {subsQuery.data?.map((s) => <option key={s.subscriptionId} value={s.subscriptionId}>{s.displayName} ({s.subscriptionId.slice(0, 8)}…)</option>)}
                </select>
              )}
              <ErrorMsg msg={errors.subscriptionId} />
            </label>
          </div>)}

          {/* Step 2 */}
          {step === 2 && (<div>
            <h2 style={{ fontSize: 14, fontWeight: 600, marginBottom: 4 }}>Resource Groups & Region</h2>
            <p style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 14, lineHeight: 1.5 }}>
              Configure where your Azure resources will be deployed.
            </p>

            {/* Region */}
            <label style={{ display: "block", marginBottom: 14 }}>
              <span className="glass-label">
                Region
                <Tooltip content={<>
                  <strong>Azure Region</strong><br />
                  All resources (AKS, Storage, ACR, VM) are created in this region.
                  Choose the one closest to your data for best performance.
                  <div className="tt-note">Tip: koreacentral is recommended for Korea-based researchers.</div>
                </>} />
              </span>
              <select className="glass-input" value={config.region} onChange={(e) => setConfig((c) => ({ ...c, region: e.target.value }))}>
                {REGIONS.map((r) => <option key={r} value={r}>{r}</option>)}
              </select>
              <ErrorMsg msg={errors.region} />
            </label>

            {rgQuery.isLoading ? (
              <div style={{ display: "flex", alignItems: "center", gap: 8, color: "var(--text-muted)" }}>
                <Loader2 size={14} className="spin" /> Loading resource groups...
              </div>
            ) : (<>
              {rgQuery.isError && (
                <div style={{ color: "var(--warning)", fontSize: 12, lineHeight: 1.5, marginBottom: 10 }}>
                  Could not list resource groups. Enter names manually:
                </div>
              )}

              {/* ── BLAST Workload ── */}
              <div className="wiz-section-header">
                <span className="wiz-section-icon">🔬</span>
                BLAST Workload
              </div>

              <RgField
                label="Workload Resource Group"
                configKey="workloadResourceGroup"
                placeholder="rg-elb"
                config={config}
                setConfig={setConfig}
                rgData={rgQuery.data}
                isManual={rgQuery.isError || !rgQuery.data?.length}
                error={errors.workloadResourceGroup}
                tooltip={<>
                  <strong>Workload Resource Group</strong><br />
                  Contains the resources for running BLAST searches.
                  <div className="tt-resources">
                    <div className="tt-resource"><span className="tt-icon">☸</span> <strong>AKS Cluster</strong> — runs BLAST jobs on Kubernetes</div>
                    <div className="tt-resource"><span className="tt-icon">🗄</span> <strong>Storage Account</strong> — holds blast-db, queries, and results</div>
                  </div>
                  <div className="tt-note">
                    Tip: Create separate RGs for different projects (e.g. rg-elb-projectA, rg-elb-projectB).
                    Each gets its own AKS + Storage.
                  </div>
                </>}
              />

              {/* ── Shared Infrastructure ── */}
              <div className="wiz-section-header">
                <span className="wiz-section-icon">🏗</span>
                Shared Infrastructure
                <span className="wiz-shared-badge">shared across all workloads</span>
              </div>

              <RgField
                label="ACR Resource Group"
                configKey="acrResourceGroup"
                placeholder="rg-elbacr"
                config={config}
                setConfig={setConfig}
                rgData={rgQuery.data}
                isManual={rgQuery.isError || !rgQuery.data?.length}
                error={errors.acrResourceGroup}
                tooltip={<>
                  <strong>Container Registry (ACR)</strong><br />
                  Holds the pre-built Docker images needed by ElasticBLAST.
                  <div className="tt-resources">
                    <div className="tt-resource"><span className="tt-icon">📦</span> <strong>ncbi/elb</strong> — ElasticBLAST runtime (1.4.0)</div>
                    <div className="tt-resource"><span className="tt-icon">📦</span> <strong>ncbi/elb-job-submit</strong> — Job submission (4.1.0)</div>
                    <div className="tt-resource"><span className="tt-icon">📦</span> <strong>ncbi/elb-query-split</strong> — Query splitter (0.1.4)</div>
                  </div>
                  <div className="tt-note">
                    You only need one ACR. It is shared by all workload RGs.
                    Images are built once and reused.
                  </div>
                </>}
              />

              <RgField
                label="Terminal Resource Group"
                configKey="terminalResourceGroup"
                placeholder="rg-elb-terminal"
                config={config}
                setConfig={setConfig}
                rgData={rgQuery.data}
                isManual={rgQuery.isError || !rgQuery.data?.length}
                error={errors.terminalResourceGroup}
                tooltip={<>
                  <strong>Remote Terminal VM</strong><br />
                  A Linux VM with everything pre-installed for running elastic-blast CLI.
                  <div className="tt-resources">
                    <div className="tt-resource"><span className="tt-icon">🖥</span> <strong>Terminal VM</strong> — Ubuntu 22.04, D4s_v5</div>
                    <div className="tt-resource"><span className="tt-icon">🔧</span> Pre-installed: az CLI, kubectl, azcopy, Python 3.11</div>
                  </div>
                  <div className="tt-note">
                    You SSH into this VM and run <code>az login --use-device-code</code> to start working.
                    One terminal serves all your workloads.
                  </div>
                </>}
              />
            </>)}
          </div>)}

          {/* Step 3: Discover & Create */}
          {step === 3 && (<div>
            <h2 style={{ fontSize: 14, fontWeight: 600, marginBottom: 4 }}>Discover & Create Resources</h2>
            <p style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 14, lineHeight: 1.5 }}>
              We scan for existing resources. Missing ones can be created here.
            </p>

            {/* Storage */}
            <ResourceRow
              label={`Storage Account (${config.workloadResourceGroup})`}
              icon="🗄" placeholder="elbstorage01"
              value={config.storageAccountName}
              onChange={(v) => setConfig((c) => ({ ...c, storageAccountName: v }))}
              query={storageQuery}
              nameKey="name"
              isValid={!config.storageAccountName || STORAGE_RE.test(config.storageAccountName)}
              mutation={createStorageMut}
              error={errors.storageAccountName}
            />

            {/* ACR */}
            <ResourceRow
              label={`Container Registry (${config.acrResourceGroup})`}
              icon="📦" placeholder="elbacr"
              value={config.acrName}
              onChange={(v) => setConfig((c) => ({ ...c, acrName: v }))}
              query={acrQuery}
              nameKey="name"
              isValid={!config.acrName || ACR_RE.test(config.acrName)}
              mutation={createAcrMut}
              error={errors.acrName}
            />

            {/* Terminal VM */}
            <div style={{ fontSize: 11, color: "var(--text-faint)", textTransform: "uppercase", letterSpacing: "0.06em", margin: "14px 0 6px" }}>
              Terminal VM ({config.terminalResourceGroup})
            </div>
            <div style={{ background: "var(--bg-secondary)", border: "1px solid var(--border-weak)", borderRadius: "var(--radius)", padding: "12px 14px", display: "flex", alignItems: "center", gap: 10 }}>
              <div style={{ fontSize: 16 }}>🖥</div>
              <input className="glass-input" placeholder="vm-elb-terminal" value={config.terminalVmName} onChange={(e) => setConfig((c) => ({ ...c, terminalVmName: e.target.value.trim() }))} spellCheck={false} style={{ flex: 1, fontSize: 12 }} />
              {!vmQuery.isLoading && config.terminalVmName && vmQuery.data?.some((v) => v.name === config.terminalVmName) ? (
                <span className="gt gt-g"><CheckCircle2 size={10} /> Found</span>
              ) : <span className="gt gt-o">Provision later</span>}
            </div>
            <ErrorMsg msg={errors.terminalVmName} />

            <div style={{ marginTop: 16, padding: "12px 14px", background: "rgba(110,159,255,0.06)", border: "1px solid rgba(110,159,255,0.15)", borderRadius: "var(--radius)", fontSize: 12, color: "var(--text-muted)", lineHeight: 1.5 }}>
              Config is saved in your browser. Change it anytime via the ⚙ icon.
            </div>
          </div>)}

          {/* Step 4: Confirm */}
          {step === 4 && (<div>
            <h2 style={{ fontSize: 14, fontWeight: 600, marginBottom: 4 }}>Confirm Setup</h2>
            <p style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 14, lineHeight: 1.5 }}>Review your configuration.</p>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <tbody>
                {([
                  ["Subscription", config.subscriptionId],
                  ["Region", config.region],
                  ["Workload RG", config.workloadResourceGroup],
                  ["Storage", config.storageAccountName || "— (skip)"],
                  ["ACR RG", config.acrResourceGroup],
                  ["ACR", config.acrName || "— (skip)"],
                  ["Terminal RG", config.terminalResourceGroup],
                  ["Terminal VM", config.terminalVmName || "— (skip)"],
                ] as const).map(([l, v]) => (
                  <tr key={l}>
                    <td style={{ padding: "8px 0", borderBottom: "1px solid var(--border-weak)", color: "var(--text-muted)", width: 160 }}>{l}</td>
                    <td style={{ padding: "8px 0", borderBottom: "1px solid var(--border-weak)", fontWeight: 500, fontFamily: "var(--font-mono)", fontSize: 12, color: v.startsWith("—") ? "var(--text-faint)" : "var(--text-primary)" }}>{v}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>)}
        </div>

        {/* Footer */}
        <div style={{ padding: "16px 32px", borderTop: "1px solid var(--border-weak)", display: "flex", justifyContent: "space-between", alignItems: "center", background: "var(--bg-secondary)" }}>
          {step > 1 ? <button className="glass-button" onClick={() => { setStep((s) => (s - 1) as Step); setAttempted(false); setErrors({}); }}>← Back</button> : <div />}
          <div style={{ display: "flex", gap: 8 }}>
            {step < 4 ? (
              <button className="glass-button glass-button--primary" onClick={handleNext}>Next →</button>
            ) : (
              <button className="glass-button" style={{ background: "rgba(115,191,105,0.12)", borderColor: "rgba(115,191,105,0.35)", color: "var(--success)" }} onClick={handleFinish}>Save & Open Dashboard →</button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// ResourceRow sub-component for Step 3
// ---------------------------------------------------------------------------
interface ResourceRowProps {
  label: string;
  icon: string;
  placeholder: string;
  value: string;
  onChange: (v: string) => void;
  query: { isLoading: boolean; isError: boolean; data?: Array<{ name: string }> | undefined };
  nameKey: string;
  isValid: boolean;
  mutation: { isPending: boolean; isSuccess: boolean; isError: boolean; error: Error | null; mutate: () => void };
  error?: string;
}

function ResourceRow({ label, icon, placeholder, value, onChange, query, isValid, mutation, error }: ResourceRowProps) {
  const found = !query.isLoading && value && query.data?.some((r) => r.name === value);

  return (
    <>
      <div style={{ fontSize: 11, color: "var(--text-faint)", textTransform: "uppercase", letterSpacing: "0.06em", margin: "14px 0 6px" }}>{label}</div>
      <div style={{ background: "var(--bg-secondary)", border: "1px solid var(--border-weak)", borderRadius: "var(--radius)", padding: "12px 14px", display: "flex", alignItems: "center", gap: 10 }}>
        <div style={{ fontSize: 16 }}>{icon}</div>
        {query.isError || (!query.isLoading && !query.data?.length) ? (
          <input className="glass-input" placeholder={placeholder} value={value} onChange={(e) => onChange(e.target.value.trim())} spellCheck={false} style={{ flex: 1, fontSize: 12 }} />
        ) : query.isLoading ? (
          <div style={{ flex: 1, color: "var(--text-muted)", display: "flex", alignItems: "center", gap: 6 }}><Loader2 size={14} className="spin" /> Scanning...</div>
        ) : (
          <select className="glass-input" value={value} onChange={(e) => onChange(e.target.value)} style={{ flex: 1 }}>
            {query.data!.map((r) => <option key={r.name} value={r.name}>{r.name}</option>)}
          </select>
        )}
        {found ? (
          <span className="gt gt-g"><CheckCircle2 size={10} /> Found</span>
        ) : !query.isLoading && value ? (
          <button
            className="glass-button glass-button--primary"
            style={{ fontSize: 11, padding: "3px 8px", whiteSpace: "nowrap" }}
            disabled={!isValid || mutation.isPending}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending ? <Loader2 size={10} className="spin" /> :
             mutation.isSuccess ? <><CheckCircle2 size={10} /> Created</> :
             <><Plus size={10} /> Create</>}
          </button>
        ) : null}
      </div>
      <ErrorMsg msg={error} />
      {mutation.isError && <div style={{ fontSize: 11, color: "var(--danger)", marginTop: 4 }}>{(mutation.error as Error).message}</div>}
    </>
  );
}

// ---------------------------------------------------------------------------
// RgField sub-component for Step 2 — RG selector with tooltip
// ---------------------------------------------------------------------------
interface RgFieldProps {
  label: string;
  configKey: "workloadResourceGroup" | "acrResourceGroup" | "terminalResourceGroup";
  placeholder: string;
  config: ResourceConfig;
  setConfig: React.Dispatch<React.SetStateAction<ResourceConfig>>;
  rgData: Array<{ name: string; location: string }> | undefined;
  isManual: boolean;
  error?: string;
  tooltip: ReactNode;
}

function RgField({ label, configKey, placeholder, config, setConfig, rgData, isManual, error, tooltip }: RgFieldProps) {
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState(placeholder);

  const createMut = useMutation({
    mutationFn: () => resourceApi.ensureRg({
      subscription_id: config.subscriptionId,
      resource_group: newName,
      region: config.region,
    }),
    onSuccess: () => {
      setConfig((c) => ({ ...c, [configKey]: newName }));
      setCreating(false);
    },
  });

  const nameValid = RG_RE.test(newName) && newName.length > 0;

  return (
    <div style={{ marginBottom: 12 }}>
      <span className="glass-label">
        {label}
        <Tooltip content={tooltip} width={340} />
      </span>

      {creating ? (
        /* ── Create-new mode ── */
        <div style={{ display: "flex", gap: 6, alignItems: "flex-start" }}>
          <div style={{ flex: 1 }}>
            <input
              className="glass-input"
              placeholder={placeholder}
              value={newName}
              onChange={(e) => setNewName(e.target.value.trim())}
              spellCheck={false}
              autoFocus
            />
            {!nameValid && newName && (
              <div style={{ display: "flex", alignItems: "center", gap: 4, color: "var(--danger)", fontSize: 11, marginTop: 4 }}>
                <AlertTriangle size={11} /> Letters, numbers, hyphens, underscores, periods only
              </div>
            )}
            {createMut.isError && (
              <div style={{ fontSize: 11, color: "var(--danger)", marginTop: 4 }}>
                {(createMut.error as Error).message}
              </div>
            )}
          </div>
          <button
            className="glass-button glass-button--primary"
            style={{ padding: "7px 14px", fontSize: 12, whiteSpace: "nowrap", marginTop: 0 }}
            disabled={!nameValid || createMut.isPending}
            onClick={() => createMut.mutate()}
          >
            {createMut.isPending ? <><Loader2 size={12} className="spin" /> Creating...</> :
             createMut.isSuccess ? <><CheckCircle2 size={12} /> Created</> :
             <><Plus size={12} /> Create</>}
          </button>
          <button
            className="glass-button"
            style={{ padding: "7px 10px", fontSize: 12 }}
            onClick={() => { setCreating(false); createMut.reset(); }}
          >
            Cancel
          </button>
        </div>
      ) : isManual ? (
        /* ── Manual input mode (ARM unavailable) ── */
        <input
          className="glass-input"
          placeholder={placeholder}
          value={config[configKey]}
          onChange={(e) => setConfig((c) => ({ ...c, [configKey]: e.target.value.trim() }))}
          spellCheck={false}
        />
      ) : (
        /* ── Select from existing + Create new button ── */
        <div style={{ display: "flex", gap: 6 }}>
          <select
            className="glass-input"
            style={{ flex: 1 }}
            value={config[configKey]}
            onChange={(e) => setConfig((c) => ({ ...c, [configKey]: e.target.value }))}
          >
            <option value="">Select...</option>
            {rgData?.map((g) => (
              <option key={g.name} value={g.name}>{g.name} · {g.location}</option>
            ))}
          </select>
          <button
            className="glass-button"
            style={{ padding: "7px 10px", fontSize: 11, whiteSpace: "nowrap" }}
            onClick={() => { setCreating(true); setNewName(placeholder); createMut.reset(); }}
          >
            <Plus size={12} /> New
          </button>
        </div>
      )}
      <ErrorMsg msg={error} />
    </div>
  );
}
