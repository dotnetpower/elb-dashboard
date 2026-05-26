import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  AlertCircle,
  CheckCircle2,
  Eye,
  EyeOff,
  Gauge,
  Loader2,
  Monitor,
  Moon,
  FlaskConical,
  RotateCcw,
  Settings as SettingsIcon,
  Sun,
  X,
} from "lucide-react";

import { formatApiError } from "@/api/client";
import { settingsApi } from "@/api/settings";
import { tasksApi, type TaskStatusResponse } from "@/api/tasks";
import { clearConfig, loadSavedConfig, type ResourceConfig } from "@/components/SetupWizard";
import { useAppInsights } from "@/hooks/useAppInsights";
import { useFocusTrap } from "@/hooks/useFocusTrap";
import { usePreferences, type ThemeMode } from "@/hooks/usePreferences";
import { useSidecarMetrics, type SidecarMetric } from "@/hooks/useSidecarMetrics";
import { useTheme } from "@/hooks/useTheme";

type SectionId = "appearance" | "preview" | "telemetry" | "aks" | "sizing" | "resources";
type TaskState = { taskId: string; status: TaskStatusResponse["status"]; message?: string };

type ProvisionFormState = {
  subscription_id: string;
  resource_group: string;
  component_name: string;
  region: string;
  workspace_name: string;
};

const SECTIONS: Array<{ id: SectionId; label: string; icon: React.ReactNode }> = [
  { id: "appearance", label: "Appearance", icon: <Sun size={14} strokeWidth={1.5} /> },
  { id: "preview", label: "Preview", icon: <FlaskConical size={14} strokeWidth={1.5} /> },
  { id: "telemetry", label: "Telemetry", icon: <Activity size={14} strokeWidth={1.5} /> },
  { id: "aks", label: "AKS Observability", icon: <Monitor size={14} strokeWidth={1.5} /> },
  { id: "sizing", label: "Sizing", icon: <Gauge size={14} strokeWidth={1.5} /> },
  { id: "resources", label: "Resources", icon: <SettingsIcon size={14} strokeWidth={1.5} /> },
];

const SIDECAR_RESOURCES: Record<string, { cpu: number; memoryGi: number }> = {
  api: { cpu: 0.5, memoryGi: 1.0 },
  frontend: { cpu: 0.25, memoryGi: 0.5 },
  worker: { cpu: 0.5, memoryGi: 1.0 },
  beat: { cpu: 0.25, memoryGi: 0.5 },
  redis: { cpu: 0.25, memoryGi: 0.5 },
  terminal: { cpu: 0.5, memoryGi: 1.0 },
};

const CONSUMPTION_PAIRS = [
  { cpu: 0.25, memoryGi: 0.5 },
  { cpu: 0.5, memoryGi: 1.0 },
  { cpu: 0.75, memoryGi: 1.5 },
  { cpu: 1.0, memoryGi: 2.0 },
  { cpu: 1.25, memoryGi: 2.5 },
  { cpu: 1.5, memoryGi: 3.0 },
  { cpu: 1.75, memoryGi: 3.5 },
  { cpu: 2.0, memoryGi: 4.0 },
  { cpu: 2.25, memoryGi: 4.5 },
  { cpu: 2.5, memoryGi: 5.0 },
  { cpu: 2.75, memoryGi: 5.5 },
  { cpu: 3.0, memoryGi: 6.0 },
  { cpu: 3.25, memoryGi: 6.5 },
  { cpu: 3.5, memoryGi: 7.0 },
  { cpu: 3.75, memoryGi: 7.5 },
  { cpu: 4.0, memoryGi: 8.0 },
];

type SizingSeverity = "ok" | "watch" | "scale";

type SidecarSizingSignal = {
  name: string;
  health: SidecarMetric["health"] | "missing";
  cpuLimit: number;
  memoryLimitGi: number;
  cpuUtilPct: number | null;
  memoryUtilPct: number | null;
  severity: SizingSeverity;
};

type SizingSample = {
  ts: number;
  signals: SidecarSizingSignal[];
};

const SIZING_HISTORY_LIMIT = 6;
const SIZING_SCALE_HIT_THRESHOLD = 3;

interface Props {
  open: boolean;
  onClose: () => void;
}

export function SettingsPanel({ open, onClose }: Props) {
  const trapRef = useFocusTrap<HTMLDivElement>(open);
  const [active, setActive] = useState<SectionId>("appearance");
  const { reset } = usePreferences();
  const config = useMemo<ResourceConfig | null>(() => (open ? loadSavedConfig() : null), [open]);
  const showFooterActions = active !== "preview";

  useEffect(() => {
    if (!open) return;
    const handler = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <>
      <div
        onClick={onClose}
        style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.42)", zIndex: 59 }}
      />
      <aside
        role="dialog"
        aria-modal="true"
        aria-label="Settings"
        ref={trapRef}
        style={{
          position: "fixed",
          top: 0,
          right: 0,
          bottom: 0,
          width: "min(720px, calc(100vw - 24px))",
          background: "var(--bg-primary)",
          borderLeft: "1px solid var(--border-medium)",
          boxShadow: "-12px 0 40px rgba(0,0,0,0.45)",
          zIndex: 60,
          display: "grid",
          gridTemplateRows: "56px 1fr 60px",
        }}
      >
        <header
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            padding: "0 20px",
            borderBottom: "1px solid var(--border-weak)",
          }}
        >
          <h2 style={{ fontSize: 14, fontWeight: 600, margin: 0, display: "flex", gap: 8 }}>
            <SettingsIcon size={16} strokeWidth={1.5} /> Settings
          </h2>
          <IconButton label="Close settings" onClick={onClose}>
            <X size={16} />
          </IconButton>
        </header>

        <div style={{ display: "grid", gridTemplateColumns: "180px 1fr", minHeight: 0 }}>
          <nav
            aria-label="Settings sections"
            style={{ borderRight: "1px solid var(--border-weak)", padding: "12px 8px" }}
          >
            {SECTIONS.map((section) => {
              const selected = section.id === active;
              return (
                <button
                  key={section.id}
                  onClick={() => setActive(section.id)}
                  aria-current={selected ? "page" : undefined}
                  style={{
                    width: "100%",
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                    padding: "8px 12px",
                    borderRadius: 6,
                    border: "none",
                    cursor: "pointer",
                    textAlign: "left",
                    color: selected ? "var(--text-primary)" : "var(--text-muted)",
                    background: selected ? "var(--bg-hover)" : "transparent",
                    boxShadow: selected ? "inset 2px 0 0 var(--accent)" : "none",
                    fontSize: 12,
                  }}
                >
                  {section.icon}
                  {section.label}
                </button>
              );
            })}
          </nav>

          <main style={{ padding: "20px 24px", overflowY: "auto" }}>
            {active === "appearance" && <AppearanceSection />}
            {active === "preview" && <PreviewSection />}
            {active === "telemetry" && <TelemetrySection config={config} />}
            {active === "aks" && <AksSection config={config} />}
            {active === "sizing" && <SizingSection />}
            {active === "resources" && <ResourcesSection config={config} />}
          </main>
        </div>

        <footer
          style={{
            padding: "12px 20px",
            borderTop: "1px solid var(--border-weak)",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 12,
          }}
        >
          <span style={{ color: "var(--text-faint)", fontSize: 11 }}>
            Stored locally · <code>elb-prefs</code>
          </span>
          {showFooterActions && (
            <div style={{ display: "flex", gap: 8 }}>
              <button
                className="glass-button"
                onClick={() => {
                  if (window.confirm("Reset theme, preview features, telemetry, and connection string preferences?")) {
                    reset();
                  }
                }}
                style={{ fontSize: 12 }}
              >
                Reset
              </button>
            </div>
          )}
        </footer>
      </aside>
    </>
  );
}

function AppearanceSection() {
  const { theme, setTheme } = useTheme();
  return (
    <Section heading="Appearance">
      <Group>
        <Row
          label="Theme"
          hint="Choose a fixed palette or follow your OS preference."
          control={
            <Segmented<ThemeMode>
              ariaLabel="Theme"
              value={theme}
              onChange={setTheme}
              options={[
                { value: "light", label: <><Sun size={12} /> Light</> },
                { value: "dark", label: <><Moon size={12} /> Dark</> },
                { value: "system", label: <><Monitor size={12} /> System</> },
              ]}
            />
          }
        />
      </Group>
    </Section>
  );
}

function PreviewSection() {
  const { prefs, setPref } = usePreferences();
  return (
    <Section heading="Preview">
      <Group>
        <Row
          label="Custom DB"
          hint="Show the custom database builder route and navigation entry. Disabled by default."
          control={
            <Toggle
              checked={prefs.previewCustomDbEnabled}
              onChange={(value) => setPref("previewCustomDbEnabled", value)}
              label="Custom DB preview"
            />
          }
        />
        <Row
          label="Lab Tools"
          hint="Show Lab Tools in the top navigation. Disabled by default."
          control={
            <Toggle
              checked={prefs.previewLabToolsEnabled}
              onChange={(value) => setPref("previewLabToolsEnabled", value)}
              label="Lab Tools preview"
            />
          }
        />
        <Row
          label="Live Wall"
          hint="Show the Live Wall monitor route and navigation entry. Disabled by default."
          control={
            <Toggle
              checked={prefs.previewLiveWallEnabled}
              onChange={(value) => setPref("previewLiveWallEnabled", value)}
              label="Live Wall preview"
            />
          }
        />
      </Group>
      <StatusLine kind="info">
        Preview selections are stored in this browser only and take effect immediately.
      </StatusLine>
    </Section>
  );
}

function TelemetrySection({ config }: { config: ResourceConfig | null }) {
  const { prefs, setPref } = usePreferences();
  const ai = useAppInsights();
  const [showSecret, setShowSecret] = useState(false);
  const [testMessage, setTestMessage] = useState<{ kind: "success" | "error"; text: string } | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [task, setTask] = useState<TaskState | null>(null);
  const [applyTask, setApplyTask] = useState<TaskState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [applyError, setApplyError] = useState<string | null>(null);
  const [form, setForm] = useState<ProvisionFormState>(() => defaultProvisionForm(config));
  const lastAutoAppliedConnectionString = useRef("");

  useEffect(() => {
    setForm((prev) => ({
      ...prev,
      subscription_id: prev.subscription_id || config?.subscriptionId || "",
      resource_group: prev.resource_group || config?.workloadResourceGroup || "",
      region: prev.region || config?.region || "koreacentral",
    }));
  }, [config]);

  usePollTask(task, setTask, (status) => {
    if (status.status !== "SUCCESS") return;
    const result = status.result as {
      connection_string?: string;
      component?: { workspace_resource_id?: string };
      workspace?: { id?: string };
      deployment_apply?: { status?: string; reason?: string };
    } | null;
    if (result?.connection_string) {
      setPref("appInsightsConnectionString", result.connection_string);
      setPref("telemetryEnabled", true);
    }
    const workspaceId = result?.component?.workspace_resource_id || result?.workspace?.id;
    if (workspaceId) {
      setPref("appInsightsWorkspaceResourceId", workspaceId);
    }
    if (result?.connection_string || workspaceId) {
      const serverApplied = result?.deployment_apply?.status === "applied";
      setTask((prev) => prev && { ...prev, message: serverApplied ? "App Insights, workspace, and server telemetry applied." : "App Insights and workspace applied." });
    }
  });

  usePollTask(applyTask, setApplyTask, (status) => {
    if (status.status === "FAILURE") {
      lastAutoAppliedConnectionString.current = "";
      return;
    }
    if (status.status !== "SUCCESS") return;
    const result = status.result as { deployment_apply?: { status?: string; reason?: string; revision?: string | null } } | null;
    const applyStatus = result?.deployment_apply?.status;
    const revision = result?.deployment_apply?.revision;
    if (applyStatus !== "applied") {
      lastAutoAppliedConnectionString.current = "";
    }
    setApplyTask((prev) => prev && {
      ...prev,
      message: applyStatus === "applied"
        ? `Server telemetry applied${revision ? ` (${revision})` : ""}.`
        : `Server telemetry not applied (${result?.deployment_apply?.reason ?? "skipped"}).`,
    });
  });

  const applyToDeployment = useCallback(async (connectionString?: string): Promise<boolean> => {
    const value = (connectionString ?? prefs.appInsightsConnectionString).trim();
    if (!value) {
      setApplyError("Enter an Application Insights connection string first.");
      return false;
    }
    setApplyError(null);
    setApplyTask(null);
    try {
      const response = await settingsApi.applyAppInsightsToDeployment({ connection_string: value });
      setApplyTask({ taskId: response.task_id, status: "PENDING", message: "Applying server telemetry" });
      return true;
    } catch (err) {
      setApplyError(formatApiError(err, "arm"));
      return false;
    }
  }, [prefs.appInsightsConnectionString]);

  const handleTelemetryToggle = useCallback((enabled: boolean) => {
    setPref("telemetryEnabled", enabled);
    const userConnectionString = prefs.appInsightsConnectionString.trim();
    if (enabled && userConnectionString) {
      void applyToDeployment(userConnectionString);
    }
  }, [applyToDeployment, prefs.appInsightsConnectionString, setPref]);

  useEffect(() => {
    const value = prefs.appInsightsConnectionString.trim();
    if (!value) return;
    // Avoid sending partial manual input. A modern App Insights connection
    // string includes both fields; provisioning still sets the same shape.
    if (!value.includes("InstrumentationKey=") || !value.includes("IngestionEndpoint=")) {
      return;
    }
    if (value === lastAutoAppliedConnectionString.current) return;
    if (isRunningTask(applyTask)) return;
    const timer = window.setTimeout(() => {
      void applyToDeployment(value).then((queued) => {
        if (queued) {
          lastAutoAppliedConnectionString.current = value;
        }
      });
    }, 900);
    return () => window.clearTimeout(timer);
  }, [applyTask, applyToDeployment, prefs.appInsightsConnectionString]);

  const sendTest = useCallback(() => {
    if (!ai.active) {
      setTestMessage({ kind: "error", text: "Telemetry is off or no connection string is configured." });
      return;
    }
    try {
      ai.trackPageView({ name: "settings.telemetry.test" });
      setTestMessage({ kind: "success", text: "Test event sent. It should appear in App Insights within a few minutes." });
    } catch (err) {
      setTestMessage({ kind: "error", text: formatApiError(err) });
    }
  }, [ai]);

  const provision = useCallback(async () => {
    setError(null);
    setTask(null);
    try {
      const response = await settingsApi.provisionAppInsights(form);
      setTask({ taskId: response.task_id, status: "PENDING" });
    } catch (err) {
      setError(formatApiError(err, "arm"));
    }
  }, [form]);

  return (
    <Section heading="Telemetry">
      <Group>
        <Row
          label="Send application telemetry to App Insights"
          hint="Browser telemetry starts immediately. Connection string edits are applied to api, worker, and beat automatically."
          control={<Toggle checked={prefs.telemetryEnabled} onChange={handleTelemetryToggle} label="Application telemetry" />}
        />
        <Row
          label="Effective source"
          hint={ai.source === "deployment" ? "Using APPLICATIONINSIGHTS_CONNECTION_STRING from the deployment." : ai.source === "user" ? "Using the manually entered connection string below." : "Enter a connection string or provision a new resource."}
          control={<Badge tone={ai.active ? "success" : "muted"}>{ai.active ? "Active" : ai.source}</Badge>}
        />
      </Group>

      <Group title="Connection string override">
        <Field label="Application Insights connection string" hint="Leave blank to use the deployment-provided connection string. A complete value is applied to server sidecars automatically.">
          <div style={{ display: "flex", gap: 6 }}>
            <input
              type={showSecret ? "text" : "password"}
              value={prefs.appInsightsConnectionString}
              onChange={(event) => setPref("appInsightsConnectionString", event.target.value)}
              placeholder="InstrumentationKey=...;IngestionEndpoint=https://..."
              autoComplete="off"
              spellCheck={false}
              style={INPUT_STYLE}
            />
            <IconButton label={showSecret ? "Hide connection string" : "Show connection string"} onClick={() => setShowSecret((p) => !p)}>
              {showSecret ? <EyeOff size={14} /> : <Eye size={14} />}
            </IconButton>
          </div>
        </Field>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", paddingBottom: 14 }}>
          <button className="glass-button" onClick={sendTest} style={{ fontSize: 12 }}>Send test event</button>
          {testMessage && <StatusLine kind={testMessage.kind}>{testMessage.text}</StatusLine>}
          {applyError && <StatusLine kind="error">{applyError}</StatusLine>}
          {applyTask && <TaskStatusLine task={applyTask} />}
        </div>
      </Group>

      <Group title="Provision a resource">
        <Row
          label="Create Application Insights"
          hint="Creates or reuses a Log Analytics workspace, then creates an App Insights component."
          control={<button className="glass-button" onClick={() => setFormOpen((p) => !p)} style={{ fontSize: 12 }}>{formOpen ? "Hide form" : "Open form"}</button>}
        />
        {formOpen && (
          <div style={{ paddingBottom: 14 }}>
            <ProvisionForm value={form} onChange={setForm} onSubmit={provision} busy={isRunningTask(task)} />
            {error && <StatusLine kind="error">{error}</StatusLine>}
            {task && <TaskStatusLine task={task} />}
          </div>
        )}
      </Group>
    </Section>
  );
}

function AksSection({ config }: { config: ResourceConfig | null }) {
  const { prefs, setPref } = usePreferences();
  const [clusterName, setClusterName] = useState("aks-elb-e2e-core-nt");
  const [appInsightsName, setAppInsightsName] = useState("appi-elb-dashboard");
  const [status, setStatus] = useState<string | null>(null);
  const [containerInsightsEnabled, setContainerInsightsEnabled] = useState<boolean | null>(null);
  const [task, setTask] = useState<TaskState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [resolvingWorkspace, setResolvingWorkspace] = useState(false);

  usePollTask(task, setTask, (taskStatus) => {
    if (taskStatus.status !== "SUCCESS") return;
    const result = taskStatus.result as { enabled?: boolean; workspace_resource_id?: string | null } | null;
    if (typeof result?.enabled === "boolean") {
      setContainerInsightsEnabled(result.enabled);
      setStatus(
        result.enabled
          ? `Enabled (${result.workspace_resource_id ?? "workspace unknown"})`
          : "Disabled",
      );
    }
  });

  const canRead = Boolean(config?.subscriptionId && config.workloadResourceGroup && clusterName);

  const refresh = useCallback(async () => {
    if (!config || !canRead) return;
    setError(null);
    try {
      const response = await settingsApi.getAksObservabilityStatus({
        subscription_id: config.subscriptionId,
        resource_group: config.workloadResourceGroup,
        cluster_name: clusterName,
      });
      setContainerInsightsEnabled(response.enabled);
      setStatus(response.enabled ? `Enabled (${response.workspace_resource_id ?? "workspace unknown"})` : "Disabled");
      if (response.workspace_resource_id) {
        setPref("appInsightsWorkspaceResourceId", response.workspace_resource_id);
      }
    } catch (err) {
      setError(formatApiError(err, "aks"));
    }
  }, [canRead, clusterName, config, setPref]);

  useEffect(() => {
    if (!canRead) return;
    void refresh();
  }, [canRead, refresh]);

  const workspaceId = prefs.appInsightsWorkspaceResourceId.trim();

  const resolveWorkspace = useCallback(async (): Promise<string> => {
    if (!config) return "";
    setError(null);
    setResolvingWorkspace(true);
    try {
      const { component } = await settingsApi.lookupAppInsights({
        subscription_id: config.subscriptionId,
        component_name: appInsightsName,
      });
      if (component.connection_string) {
        setPref("appInsightsConnectionString", component.connection_string);
        setPref("telemetryEnabled", true);
      }
      if (!component.workspace_resource_id) {
        setError("This App Insights resource did not return a Log Analytics workspace id.");
        return "";
      }
      setPref("appInsightsWorkspaceResourceId", component.workspace_resource_id);
      setStatus(`Workspace resolved (${component.workspace_resource_id.split("/").slice(-1)[0]})`);
      return component.workspace_resource_id;
    } catch (err) {
      setError(formatApiError(err, "arm"));
      return "";
    } finally {
      setResolvingWorkspace(false);
    }
  }, [appInsightsName, config, setPref]);

  const enable = useCallback(async () => {
    if (!config) return;
    setError(null);
    setTask(null);
    let workspaceId = prefs.appInsightsWorkspaceResourceId.trim();
    if (!workspaceId) {
      workspaceId = await resolveWorkspace();
      if (!workspaceId) return;
    }
    try {
      const response = await settingsApi.enableAksObservability({
        subscription_id: config.subscriptionId,
        resource_group: config.workloadResourceGroup,
        cluster_name: clusterName,
        workspace_resource_id: workspaceId,
      });
      setTask({ taskId: response.task_id, status: "PENDING" });
    } catch (err) {
      setError(formatApiError(err, "aks"));
    }
  }, [clusterName, config, prefs.appInsightsWorkspaceResourceId, resolveWorkspace]);

  const disable = useCallback(async () => {
    if (!config) return;
    setError(null);
    setTask(null);
    try {
      const response = await settingsApi.disableAksObservability({
        subscription_id: config.subscriptionId,
        resource_group: config.workloadResourceGroup,
        cluster_name: clusterName,
      });
      setTask({ taskId: response.task_id, status: "PENDING", message: "Disabling Container Insights" });
    } catch (err) {
      setError(formatApiError(err, "aks"));
    }
  }, [clusterName, config]);

  return (
    <Section heading="AKS Observability">
      <Group>
        <Field label="AKS cluster name" hint="Container Insights is enabled by patching the omsagent addon on this cluster.">
          <input value={clusterName} onChange={(event) => setClusterName(event.target.value)} style={INPUT_STYLE} />
        </Field>
        <Field label="Application Insights resource name" hint="Used to resolve the backing Log Analytics workspace automatically.">
          <input value={appInsightsName} onChange={(event) => setAppInsightsName(event.target.value)} style={INPUT_STYLE} placeholder="appi-elb-dashboard" />
        </Field>
        <Row
          label="Log Analytics workspace"
          hint={workspaceId ? "Automatically captured from the App Insights resource." : "Use Telemetry > Provision a resource first. Existing App Insights resources are reused by name."}
          control={<Badge tone={workspaceId ? "success" : "muted"}>{workspaceId ? "Ready" : "Missing"}</Badge>}
        />
        {workspaceId && (
          <StatusLine kind="info">
            Workspace <code>{workspaceId.split("/").slice(-1)[0]}</code> will be used.
          </StatusLine>
        )}
        {!workspaceId && prefs.appInsightsConnectionString && (
          <StatusLine kind="info">
            The connection string is configured, but AKS needs the backing Log Analytics workspace. Open Telemetry and provision/reuse the App Insights resource by name to fill it automatically.
          </StatusLine>
        )}
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", paddingBottom: 14 }}>
          <button className="glass-button" onClick={resolveWorkspace} disabled={!canRead || !appInsightsName || resolvingWorkspace} style={{ fontSize: 12 }}>
            {resolvingWorkspace ? "Resolving..." : "Resolve workspace"}
          </button>
          <button className="glass-button" onClick={refresh} disabled={!canRead} style={{ fontSize: 12 }}>Refresh status</button>
          {containerInsightsEnabled ? (
            <button className="glass-button" onClick={disable} disabled={!canRead || isRunningTask(task)} style={{ fontSize: 12 }}>Disable Container Insights</button>
          ) : (
            <button className="glass-button glass-button--primary" onClick={enable} disabled={!canRead || !appInsightsName || isRunningTask(task) || resolvingWorkspace} style={{ fontSize: 12 }}>Enable Container Insights</button>
          )}
        </div>
        {status && <StatusLine kind={status.startsWith("Enabled") ? "success" : "info"}>{status}</StatusLine>}
        {error && <StatusLine kind="error">{error}</StatusLine>}
        {task && <TaskStatusLine task={task} />}
      </Group>
    </Section>
  );
}

function SizingSection() {
  const metrics = useSidecarMetrics();
  const rawSignals = useMemo(() => buildSizingSignals(metrics.data?.sidecars ?? {}), [metrics.data?.sidecars]);
  const [samples, setSamples] = useState<SizingSample[]>([]);
  const current = useMemo(() => currentConsumptionPair(), []);
  const next = useMemo(() => nextConsumptionPair(current.cpu, current.memoryGi), [current.cpu, current.memoryGi]);
  const snapshotTs = metrics.data?.ts ?? null;

  useEffect(() => {
    if (snapshotTs == null) return;
    setSamples((prev) => {
      if (prev.some((sample) => sample.ts === snapshotTs)) return prev;
      return [...prev, { ts: snapshotTs, signals: rawSignals }].slice(-SIZING_HISTORY_LIMIT);
    });
  }, [rawSignals, snapshotTs]);

  const signals = useMemo(
    () => applySustainedSizing(rawSignals, samples, snapshotTs),
    [rawSignals, samples, snapshotTs],
  );
  const hottest = signals.find((signal) => signal.severity === "scale") ?? signals.find((signal) => signal.severity === "watch") ?? null;
  const overall: SizingSeverity = signals.some((signal) => signal.severity === "scale")
    ? "scale"
    : signals.some((signal) => signal.severity === "watch") || metrics.isError
      ? "watch"
      : "ok";
  const statusKind = overall === "scale" ? "error" : overall === "watch" ? "info" : "success";
  const statusText = overall === "scale"
    ? "Scale up recommended"
    : overall === "watch"
      ? "Watch current load"
      : "Current size looks healthy";

  return (
    <Section heading="Control Plane Sizing">
      <Group>
        <Row
          label="Recommendation"
          hint={hottest ? `${hottest.name} is the current pressure point.` : "Based on live sidecar CPU and memory snapshots."}
          control={<SizingPill severity={overall}>{statusText}</SizingPill>}
        />
        <Row
          label="Current Consumption pair"
          hint="Azure validates the aggregate resources across all six sidecars."
          control={<code style={{ fontSize: 12 }}>{formatPair(current)}</code>}
        />
        <Row
          label="Next scale step"
          hint={next ? `Add capacity to the hottest sidecar while keeping the aggregate pair valid.` : "Already at the Consumption maximum."}
          control={<code style={{ fontSize: 12 }}>{next ? formatPair(next) : "Dedicated profile"}</code>}
        />
        <StatusLine kind={statusKind}>
          {metrics.isLoading
            ? "Waiting for the first sidecar metrics snapshot."
            : metrics.isError
              ? "Metrics are stale or unavailable; keep the current deployment but verify the sidecar reporters."
                : `${metrics.source === "live" ? "Live" : "Polling"} metrics${metrics.lastUpdated ? ` · ${metrics.lastUpdated.toLocaleTimeString()}` : ""} · ${samples.length} samples collected, ${SIZING_SCALE_HIT_THRESHOLD} needed for scale-up`}
        </StatusLine>
      </Group>

      <Group title="Sidecar pressure">
        <div style={{ display: "grid", gap: 8, padding: "12px 0" }}>
          {signals.map((signal) => (
            <div
              key={signal.name}
              style={{
                display: "grid",
                gridTemplateColumns: "96px 1fr 1fr auto",
                gap: 10,
                alignItems: "center",
                minHeight: 30,
                fontSize: 12,
              }}
            >
              <span style={{ color: "var(--text-primary)", fontWeight: 600 }}>{signal.name}</span>
              <SizingMeter label="CPU" value={signal.cpuUtilPct} limit={`${signal.cpuLimit} vCPU`} />
              <SizingMeter label="Memory" value={signal.memoryUtilPct} limit={`${signal.memoryLimitGi}Gi`} />
              <SizingPill severity={signal.severity}>{signal.severity === "scale" ? "Scale" : signal.severity === "watch" ? "Watch" : "OK"}</SizingPill>
            </div>
          ))}
        </div>
      </Group>
    </Section>
  );
}

function applySustainedSizing(
  currentSignals: SidecarSizingSignal[],
  samples: SizingSample[],
  currentTs: number | null,
): SidecarSizingSignal[] {
  const windowSamples = currentTs == null || samples.some((sample) => sample.ts === currentTs)
    ? samples
    : [...samples, { ts: currentTs, signals: currentSignals }].slice(-SIZING_HISTORY_LIMIT);

  return currentSignals.map((signal) => {
    if (signal.severity !== "scale") return signal;
    const scaleHits = windowSamples.filter((sample) => {
      const sampleSignal = sample.signals.find((candidate) => candidate.name === signal.name);
      return sampleSignal?.severity === "scale";
    }).length;
    if (scaleHits >= SIZING_SCALE_HIT_THRESHOLD) return signal;
    return { ...signal, severity: "watch" };
  });
}

function buildSizingSignals(sidecars: Record<string, SidecarMetric>): SidecarSizingSignal[] {
  return Object.entries(SIDECAR_RESOURCES).map(([name, limit]) => {
    const metric = sidecars[name];
    const cpuPct = asFiniteNumber(metric?.cpu_pct);
    const memPct = asFiniteNumber(metric?.mem_pct) ?? memoryPctFromBytes(metric, limit.memoryGi);
    const cpuUtilPct = cpuPct == null ? null : clampPct((cpuPct / (limit.cpu * 100)) * 100);
    const memoryUtilPct = memPct == null ? null : clampPct(memPct);
    const health = metric?.health ?? "missing";
    const severity = sizingSeverity(health, cpuUtilPct, memoryUtilPct);
    return {
      name,
      health,
      cpuLimit: limit.cpu,
      memoryLimitGi: limit.memoryGi,
      cpuUtilPct,
      memoryUtilPct,
      severity,
    };
  });
}

function sizingSeverity(
  health: SidecarMetric["health"] | "missing",
  cpuUtilPct: number | null,
  memoryUtilPct: number | null,
): SizingSeverity {
  if (cpuUtilPct != null && cpuUtilPct >= 85) return "scale";
  if (memoryUtilPct != null && memoryUtilPct >= 85) return "scale";
  if (health !== "ok") return "watch";
  if (cpuUtilPct != null && cpuUtilPct >= 65) return "watch";
  if (memoryUtilPct != null && memoryUtilPct >= 70) return "watch";
  return "ok";
}

function currentConsumptionPair(): { cpu: number; memoryGi: number } {
  return Object.values(SIDECAR_RESOURCES).reduce(
    (acc, value) => ({ cpu: acc.cpu + value.cpu, memoryGi: acc.memoryGi + value.memoryGi }),
    { cpu: 0, memoryGi: 0 },
  );
}

function nextConsumptionPair(cpu: number, memoryGi: number): { cpu: number; memoryGi: number } | null {
  return CONSUMPTION_PAIRS.find((pair) => pair.cpu > cpu && pair.memoryGi > memoryGi) ?? null;
}

function asFiniteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function memoryPctFromBytes(metric: SidecarMetric | undefined, memoryGi: number): number | null {
  const bytes = asFiniteNumber(metric?.mem_bytes);
  if (bytes == null || memoryGi <= 0) return null;
  return (bytes / (memoryGi * 1024 * 1024 * 1024)) * 100;
}

function clampPct(value: number): number {
  return Math.max(0, Math.min(100, Math.round(value * 10) / 10));
}

function formatPair(pair: { cpu: number; memoryGi: number }): string {
  return `${pair.cpu.toFixed(2).replace(/\.00$/, "")} CPU / ${pair.memoryGi.toFixed(1)}Gi`;
}

function SizingMeter({ label, value, limit }: { label: string; value: number | null; limit: string }) {
  const width = value == null ? 0 : value;
  const tone = value == null ? "var(--text-faint)" : value >= 85 ? "var(--danger)" : value >= 70 ? "var(--warning)" : "var(--success)";
  return (
    <div style={{ minWidth: 0 }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8, color: "var(--text-faint)", fontSize: 11, marginBottom: 4 }}>
        <span>{label}</span>
        <span>{value == null ? "No data" : `${value.toFixed(1)}%`} · {limit}</span>
      </div>
      <div style={{ height: 6, borderRadius: 999, background: "var(--bg-tertiary)", overflow: "hidden", border: "1px solid var(--border-weak)" }}>
        <div style={{ width: `${width}%`, height: "100%", background: tone }} />
      </div>
    </div>
  );
}

function SizingPill({ severity, children }: { severity: SizingSeverity; children: React.ReactNode }) {
  const color = severity === "scale" ? "var(--danger)" : severity === "watch" ? "var(--warning)" : "var(--success)";
  return (
    <span style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.04em", borderRadius: 999, padding: "2px 8px", border: "1px solid var(--border-weak)", color, background: "var(--bg-tertiary)", whiteSpace: "nowrap" }}>
      {children}
    </span>
  );
}

function ResourcesSection({ config }: { config: ResourceConfig | null }) {
  const fields = [
    ["Subscription", config?.subscriptionId || "-"],
    ["Region", config?.region || "-"],
    ["Workload Resource Group", config?.workloadResourceGroup || "-"],
    ["Storage Account", config?.storageAccountName || "-"],
    ["ACR Resource Group", config?.acrResourceGroup || "-"],
    ["ACR Name", config?.acrName || "-"],
  ];
  return (
    <Section heading="Resources">
      <Group>
        <div style={{ padding: "12px 0" }}>
          {fields.map(([label, value]) => (
            <div key={label} style={{ display: "grid", gridTemplateColumns: "150px 1fr", gap: 12, padding: "6px 0", fontSize: 12 }}>
              <span style={{ color: "var(--text-faint)" }}>{label}</span>
              <span style={{ color: value === "-" ? "var(--text-faint)" : "var(--text-muted)", wordBreak: "break-all" }}>{value}</span>
            </div>
          ))}
        </div>
        <div style={{ borderTop: "1px solid var(--border-weak)", padding: "12px 0" }}>
          <button
            className="glass-button"
            onClick={() => {
              clearConfig();
              window.location.assign("/");
            }}
          >
            <RotateCcw size={12} strokeWidth={1.5} /> Re-run Setup Wizard
          </button>
        </div>
      </Group>
    </Section>
  );
}

function ProvisionForm({ value, onChange, onSubmit, busy }: { value: ProvisionFormState; onChange: (next: ProvisionFormState) => void; onSubmit: () => void; busy: boolean }) {
  const update = (key: keyof ProvisionFormState) => (event: React.ChangeEvent<HTMLInputElement>) => onChange({ ...value, [key]: event.target.value });
  return (
    <div style={{ display: "grid", gap: 10, paddingTop: 10 }}>
      <Field label="Subscription ID"><input value={value.subscription_id} onChange={update("subscription_id")} style={INPUT_STYLE} /></Field>
      <Field label="Resource group"><input value={value.resource_group} onChange={update("resource_group")} style={INPUT_STYLE} /></Field>
      <Field label="Region"><input value={value.region} onChange={update("region")} style={INPUT_STYLE} /></Field>
      <Field label="Application Insights name"><input value={value.component_name} onChange={update("component_name")} style={INPUT_STYLE} placeholder="appi-elb-dashboard" /></Field>
      <Field label="Log Analytics workspace name"><input value={value.workspace_name} onChange={update("workspace_name")} style={INPUT_STYLE} placeholder="log-elb-dashboard" /></Field>
      <button className="glass-button glass-button--primary" onClick={onSubmit} disabled={busy} style={{ justifySelf: "start" }}>
        {busy ? <Loader2 size={13} /> : <Activity size={13} />} Provision App Insights
      </button>
    </div>
  );
}

function Section({ heading, children }: { heading: string; children: React.ReactNode }) {
  return (
    <section>
      <h3 style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.08em", color: "var(--text-faint)", margin: "0 0 12px" }}>{heading}</h3>
      {children}
    </section>
  );
}

function Group({ title, children }: { title?: string; children: React.ReactNode }) {
  return (
    <div style={{ background: "var(--bg-secondary)", border: "1px solid var(--border-weak)", borderRadius: 8, padding: "0 16px", marginBottom: 14 }}>
      {title && <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-muted)", padding: "12px 0 2px" }}>{title}</div>}
      {children}
    </div>
  );
}

function Row({ label, hint, control }: { label: React.ReactNode; hint?: React.ReactNode; control: React.ReactNode }) {
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 16, padding: "14px 0", borderBottom: "1px solid var(--border-weak)" }}>
      <div style={{ minWidth: 0 }}>
        <div style={{ fontSize: 13, color: "var(--text-primary)", marginBottom: 2 }}>{label}</div>
        {hint && <div style={{ fontSize: 12, color: "var(--text-faint)", lineHeight: 1.5 }}>{hint}</div>}
      </div>
      <div style={{ flexShrink: 0 }}>{control}</div>
    </div>
  );
}

function Field({ label, hint, children }: { label: React.ReactNode; hint?: React.ReactNode; children: React.ReactNode }) {
  return (
    <label style={{ display: "grid", gap: 6, paddingBottom: 10 }}>
      <span style={{ fontSize: 12, color: "var(--text-muted)" }}>{label}</span>
      {children}
      {hint && <span style={{ fontSize: 11, color: "var(--text-faint)", lineHeight: 1.5 }}>{hint}</span>}
    </label>
  );
}

function Segmented<T extends string>({ value, options, onChange, ariaLabel }: { value: T; options: Array<{ value: T; label: React.ReactNode }>; onChange: (next: T) => void; ariaLabel: string }) {
  return (
    <div role="group" aria-label={ariaLabel} style={{ display: "inline-flex", border: "1px solid var(--border-weak)", background: "var(--bg-tertiary)", borderRadius: 8, padding: 2, gap: 2 }}>
      {options.map((option) => {
        const selected = option.value === value;
        return (
          <button key={option.value} aria-pressed={selected} onClick={() => onChange(option.value)} style={{ display: "inline-flex", alignItems: "center", gap: 6, border: "none", borderRadius: 6, padding: "6px 10px", cursor: "pointer", background: selected ? "var(--bg-hover)" : "transparent", color: selected ? "var(--text-primary)" : "var(--text-muted)", fontSize: 12 }}>
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

function Toggle({ checked, onChange, label }: { checked: boolean; onChange: (next: boolean) => void; label: string }) {
  return (
    <button role="switch" aria-checked={checked} aria-label={label} onClick={() => onChange(!checked)} style={{ position: "relative", width: 36, height: 20, borderRadius: 999, background: checked ? "color-mix(in srgb, var(--accent) 30%, var(--bg-tertiary))" : "var(--bg-tertiary)", border: `1px solid ${checked ? "var(--border-focus)" : "var(--border-medium)"}`, cursor: "pointer", padding: 0 }}>
      <span style={{ position: "absolute", top: 2, left: 2, width: 14, height: 14, borderRadius: "50%", background: checked ? "var(--accent)" : "var(--text-muted)", transform: checked ? "translateX(16px)" : "translateX(0)", transition: "transform 120ms" }} />
    </button>
  );
}

function IconButton({ label, onClick, children }: { label: string; onClick: () => void; children: React.ReactNode }) {
  return (
    <button aria-label={label} onClick={onClick} style={{ width: 30, height: 30, display: "grid", placeItems: "center", color: "var(--text-muted)", background: "var(--bg-tertiary)", border: "1px solid var(--border-weak)", borderRadius: 6, cursor: "pointer" }}>
      {children}
    </button>
  );
}

function Badge({ tone, children }: { tone: "success" | "muted"; children: React.ReactNode }) {
  return <span style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.04em", borderRadius: 999, padding: "2px 8px", border: "1px solid var(--border-weak)", color: tone === "success" ? "var(--success)" : "var(--text-faint)", background: tone === "success" ? "rgba(115,191,105,0.08)" : "var(--bg-tertiary)" }}>{children}</span>;
}

function StatusLine({ kind, children }: { kind: "info" | "success" | "error" | "loading"; children: React.ReactNode }) {
  const icon = kind === "success" ? <CheckCircle2 size={13} color="var(--success)" /> : kind === "error" ? <AlertCircle size={13} color="var(--danger)" /> : kind === "loading" ? <Loader2 size={13} /> : <Activity size={13} />;
  return <div style={{ display: "flex", alignItems: "flex-start", gap: 8, fontSize: 12, color: "var(--text-muted)", lineHeight: 1.5, marginTop: 4 }}><span style={{ marginTop: 1 }}>{icon}</span><span style={{ wordBreak: "break-word" }}>{children}</span></div>;
}

function TaskStatusLine({ task }: { task: TaskState }) {
  const kind = task.status === "SUCCESS" ? "success" : task.status === "FAILURE" ? "error" : "loading";
  return <StatusLine kind={kind}>Task <code>{task.taskId.slice(0, 8)}...</code> · {task.status}{task.message ? ` — ${task.message}` : ""}</StatusLine>;
}

function defaultProvisionForm(config: ResourceConfig | null): ProvisionFormState {
  return {
    subscription_id: config?.subscriptionId ?? "",
    resource_group: config?.workloadResourceGroup ?? "",
    component_name: "appi-elb-dashboard",
    region: config?.region ?? "koreacentral",
    workspace_name: "log-elb-dashboard",
  };
}

function isRunningTask(task: TaskState | null): boolean {
  return Boolean(task && task.status !== "SUCCESS" && task.status !== "FAILURE");
}

function usePollTask(
  task: TaskState | null,
  setTask: React.Dispatch<React.SetStateAction<TaskState | null>>,
  onUpdate?: (status: TaskStatusResponse) => void,
) {
  useEffect(() => {
    if (!task || task.status === "SUCCESS" || task.status === "FAILURE") return;
    const id = window.setInterval(async () => {
      try {
        const status = await tasksApi.status(task.taskId);
        const progress = status.progress as { message?: string } | undefined;
        setTask({ taskId: task.taskId, status: status.status, message: progress?.message ?? status.error });
        onUpdate?.(status);
      } catch (err) {
        setTask({ taskId: task.taskId, status: "FAILURE", message: formatApiError(err) });
      }
    }, 4000);
    return () => window.clearInterval(id);
  }, [onUpdate, setTask, task]);
}

const INPUT_STYLE: React.CSSProperties = {
  background: "var(--bg-tertiary)",
  color: "var(--text-primary)",
  border: "1px solid var(--border-weak)",
  borderRadius: 6,
  padding: "8px 10px",
  fontSize: 12,
  fontFamily: "var(--font-mono)",
  width: "100%",
  boxSizing: "border-box",
};
