import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Copy,
  ExternalLink,
  Eye,
  EyeOff,
  Gauge,
  Globe,
  HelpCircle,
  Loader2,
  Monitor,
  Moon,
  FlaskConical,
  RotateCcw,
  Settings as SettingsIcon,
  Sun,
  Trash2,
  Upload,
  X,
} from "lucide-react";

import { formatApiError } from "@/api/client";
import { aksApi, type OpenApiPublicHttpsStatus } from "@/api/aks";
import { type AksClusterSummary, monitoringApi } from "@/api/monitoring";
import { settingsApi, type AppInsightsProvisionRequest } from "@/api/settings";
import { tasksApi, type TaskStatusResponse } from "@/api/tasks";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { clearConfig, loadSavedConfig, type ResourceConfig } from "@/components/SetupWizard";
import { useAppInsights } from "@/hooks/useAppInsights";
import { useFocusTrap } from "@/hooks/useFocusTrap";
import { usePreferences, type ThemeMode } from "@/hooks/usePreferences";
import { useSidecarMetrics, type SidecarMetric } from "@/hooks/useSidecarMetrics";
import { useTheme } from "@/hooks/useTheme";
import { pickPreferredCluster } from "@/utils/clusterSelection";

type SectionId =
  | "appearance"
  | "preview"
  | "telemetry"
  | "aks"
  | "public-https"
  | "sizing"
  | "resources";
type TaskState = {
  taskId: string;
  status: TaskStatusResponse["status"];
  message?: string;
  step?: number;
  totalSteps?: number;
};

type ProvisionFormState = {
  subscription_id: string;
  resource_group: string;
  component_name: string;
  region: string;
  workspace_name: string;
  workspace_resource_group: string;
  retention_days: number;
};

const KNOWN_AZURE_REGIONS = [
  "australiaeast",
  "brazilsouth",
  "canadacentral",
  "centralindia",
  "centralus",
  "eastasia",
  "eastus",
  "eastus2",
  "francecentral",
  "germanywestcentral",
  "japaneast",
  "japanwest",
  "koreacentral",
  "northcentralus",
  "northeurope",
  "norwayeast",
  "southafricanorth",
  "southcentralus",
  "southeastasia",
  "swedencentral",
  "switzerlandnorth",
  "uaenorth",
  "uksouth",
  "ukwest",
  "westeurope",
  "westus",
  "westus2",
  "westus3",
] as const;

const RETENTION_DAYS_OPTIONS = [7, 14, 30, 60, 90, 120, 180, 270, 365, 550, 730] as const;
const DEFAULT_RETENTION_DAYS = 30;

const SUBSCRIPTION_GUID_RE = /^[0-9a-fA-F-]{36}$/;
const RG_NAME_RE = /^[-\w._()]{1,90}$/;
const RESOURCE_NAME_RE = /^[a-zA-Z0-9][a-zA-Z0-9._\-]{1,254}$/;
const REGION_RE = /^[a-z][a-z0-9]{2,29}$/;

const SECTIONS: Array<{ id: SectionId; label: string; icon: React.ReactNode }> = [
  { id: "appearance", label: "Appearance", icon: <Sun size={14} strokeWidth={1.5} /> },
  { id: "preview", label: "Preview", icon: <FlaskConical size={14} strokeWidth={1.5} /> },
  { id: "telemetry", label: "Telemetry", icon: <Activity size={14} strokeWidth={1.5} /> },
  { id: "aks", label: "AKS Observability", icon: <Monitor size={14} strokeWidth={1.5} /> },
  { id: "public-https", label: "Public HTTPS", icon: <Globe size={14} strokeWidth={1.5} /> },
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
  const [resetOpen, setResetOpen] = useState(false);

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
            {active === "public-https" && <PublicHttpsSection config={config} />}
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
                onClick={() => setResetOpen(true)}
                style={{ fontSize: 12 }}
              >
                Reset
              </button>
            </div>
          )}
        </footer>
      </aside>
      <ConfirmDialog
        open={resetOpen}
        title="Reset preferences?"
        message="Reset theme, preview features, telemetry, and connection string preferences?"
        confirmLabel="Reset"
        confirmAriaLabel="Reset preferences"
        onConfirm={() => { setResetOpen(false); reset(); }}
        onCancel={() => setResetOpen(false)}
      />
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
  const [copyMessage, setCopyMessage] = useState<string | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [task, setTask] = useState<TaskState | null>(null);
  const [applyTask, setApplyTask] = useState<TaskState | null>(null);
  const [clearTask, setClearTask] = useState<TaskState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [applyError, setApplyError] = useState<string | null>(null);
  const [clearError, setClearError] = useState<string | null>(null);
  const [form, setForm] = useState<ProvisionFormState>(() => defaultProvisionForm(config));
  const [provisionConfirmOpen, setProvisionConfirmOpen] = useState(false);
  const [clearConfirmOpen, setClearConfirmOpen] = useState(false);
  const copyTimer = useRef<number | null>(null);

  useEffect(() => {
    setForm((prev) => ({
      ...prev,
      subscription_id: prev.subscription_id || config?.subscriptionId || "",
      resource_group: prev.resource_group || config?.workloadResourceGroup || "",
      region: prev.region || config?.region || "koreacentral",
    }));
  }, [config]);

  useEffect(() => () => {
    if (copyTimer.current != null) window.clearTimeout(copyTimer.current);
  }, []);

  const userConnectionString = prefs.appInsightsConnectionString.trim();
  const userKeyTail = extractInstrumentationKeyTail(userConnectionString);
  const isWellFormedUserString = isWellFormedConnectionString(userConnectionString);
  const hasConnectionStringSomewhere = ai.active || isWellFormedUserString || ai.source === "deployment";

  usePollTask(task, setTask, (status) => {
    if (status.status !== "SUCCESS") return;
    const result = status.result as {
      connection_string?: string;
      component?: { workspace_resource_id?: string };
      component_created?: boolean;
      workspace?: { id?: string };
      workspace_created?: boolean;
      deployment_apply?: { status?: string; reason?: string; revision?: string | null };
    } | null;
    if (result?.connection_string) {
      setPref("appInsightsConnectionString", result.connection_string);
      setPref("telemetryEnabled", true);
      const tail = extractInstrumentationKeyTail(result.connection_string);
      if (tail) setPref("appInsightsLastAppliedKeyTail", tail);
    }
    const workspaceId = result?.component?.workspace_resource_id || result?.workspace?.id;
    if (workspaceId) {
      setPref("appInsightsWorkspaceResourceId", workspaceId);
    }
    const serverApplied = result?.deployment_apply?.status === "applied";
    if (serverApplied && result?.deployment_apply?.revision) {
      setPref("appInsightsLastAppliedRevision", result.deployment_apply.revision);
    }
    if (result?.connection_string || workspaceId) {
      const createdParts: string[] = [];
      if (result?.component_created) createdParts.push("App Insights component created");
      else if (result?.component_created === false) createdParts.push("Existing App Insights reused");
      if (result?.workspace_created) createdParts.push("Log Analytics workspace created");
      else if (result?.workspace_created === false) createdParts.push("existing workspace reused");
      const createdSummary = createdParts.length > 0 ? `${createdParts.join("; ")}. ` : "";
      const deploymentSummary = serverApplied
        ? "Server telemetry applied."
        : "Server sidecars unchanged.";
      setTask((prev) => prev && { ...prev, message: `${createdSummary}${deploymentSummary}` });
      // Auto-collapse the form so the success status is the focal point.
      setFormOpen(false);
    }
  });

  usePollTask(applyTask, setApplyTask, (status) => {
    if (status.status !== "SUCCESS") return;
    const result = status.result as { deployment_apply?: { status?: string; reason?: string; revision?: string | null } } | null;
    const applyStatus = result?.deployment_apply?.status;
    const revision = result?.deployment_apply?.revision;
    if (applyStatus === "applied" && revision) {
      setPref("appInsightsLastAppliedRevision", revision);
      const tail = extractInstrumentationKeyTail(prefs.appInsightsConnectionString);
      if (tail) setPref("appInsightsLastAppliedKeyTail", tail);
    }
    setApplyTask((prev) => prev && {
      ...prev,
      message: applyStatus === "applied"
        ? `Server sidecars now use this connection string${revision ? ` (revision ${revision}).` : "."}`
        : `Server telemetry not applied (${result?.deployment_apply?.reason ?? "skipped"}).`,
    });
  });

  usePollTask(clearTask, setClearTask, (status) => {
    if (status.status !== "SUCCESS") return;
    const result = status.result as { deployment_clear?: { status?: string; revision?: string | null } } | null;
    const clearStatus = result?.deployment_clear?.status;
    const revision = result?.deployment_clear?.revision;
    if (clearStatus === "cleared" || clearStatus === "no_change") {
      setPref("appInsightsLastAppliedRevision", revision ?? "");
      setPref("appInsightsLastAppliedKeyTail", "");
    }
    setClearTask((prev) => prev && {
      ...prev,
      message: clearStatus === "cleared"
        ? `Server override removed${revision ? ` (revision ${revision}).` : "."}`
        : clearStatus === "no_change"
          ? "No server override was set — nothing to clear."
          : "Clear request was skipped.",
    });
  });

  const applyToDeployment = useCallback(async () => {
    if (!isWellFormedUserString) {
      setApplyError("Enter a complete connection string (InstrumentationKey + IngestionEndpoint) first.");
      return;
    }
    setApplyError(null);
    setApplyTask(null);
    try {
      const response = await settingsApi.applyAppInsightsToDeployment({ connection_string: userConnectionString });
      setApplyTask({ taskId: response.task_id, status: "PENDING", message: "Applying connection string to api, worker, and beat" });
    } catch (err) {
      setApplyError(formatApiError(err, "arm"));
    }
  }, [isWellFormedUserString, userConnectionString]);

  const clearFromDeployment = useCallback(() => {
    setClearConfirmOpen(true);
  }, []);

  const submitClearFromDeployment = useCallback(async () => {
    setClearConfirmOpen(false);
    setClearError(null);
    setClearTask(null);
    try {
      const response = await settingsApi.clearAppInsightsFromDeployment();
      setClearTask({ taskId: response.task_id, status: "PENDING", message: "Removing connection string from server sidecars" });
    } catch (err) {
      setClearError(formatApiError(err, "arm"));
    }
  }, []);

  const handleTelemetryToggle = useCallback(
    (enabled: boolean) => {
      if (enabled && !hasConnectionStringSomewhere) {
        setPref("telemetryEnabled", false);
        setTestMessage({
          kind: "error",
          text: "Add a connection string (or provision an App Insights resource) before enabling telemetry.",
        });
        return;
      }
      setTestMessage(null);
      setPref("telemetryEnabled", enabled);
    },
    [hasConnectionStringSomewhere, setPref],
  );

  const sendTest = useCallback(() => {
    if (!ai.active) {
      setTestMessage({ kind: "error", text: "Enable telemetry and configure a connection string first." });
      return;
    }
    try {
      ai.trackPageView({ name: "settings.telemetry.test" });
      setTestMessage({ kind: "success", text: "Test event sent. It should appear in App Insights within a few minutes." });
    } catch (err) {
      setTestMessage({ kind: "error", text: formatApiError(err) });
    }
  }, [ai]);

  const provision = useCallback(() => {
    const validation = validateProvisionForm(form);
    if (!validation.ok) {
      setError(validation.message);
      return;
    }
    setError(null);
    setProvisionConfirmOpen(true);
  }, [form]);

  const submitProvision = useCallback(async () => {
    setProvisionConfirmOpen(false);
    const ws_rg = form.workspace_resource_group.trim();
    setError(null);
    setTask(null);
    try {
      const payload: AppInsightsProvisionRequest = {
        subscription_id: form.subscription_id.trim(),
        resource_group: form.resource_group.trim(),
        component_name: form.component_name.trim(),
        region: form.region.trim(),
        workspace_name: form.workspace_name.trim(),
        retention_days: form.retention_days,
      };
      if (ws_rg) payload.workspace_resource_group = ws_rg;
      const response = await settingsApi.provisionAppInsights(payload);
      setTask({ taskId: response.task_id, status: "PENDING", message: "Submitted to background worker" });
    } catch (err) {
      setError(formatApiError(err, "arm"));
    }
  }, [form]);

  const provisionSummaryLines = useMemo(() => {
    const ws_rg = form.workspace_resource_group.trim();
    return [
      `Resource group: ${form.resource_group}`,
      `App Insights: ${form.component_name} (${form.region})`,
      `Log Analytics: ${form.workspace_name}${ws_rg && ws_rg !== form.resource_group ? ` in ${ws_rg}` : ""}`,
      `Retention: ${form.retention_days} days`,
    ];
  }, [form]);

  const copyConnectionString = useCallback(async () => {
    if (!userConnectionString) return;
    try {
      await navigator.clipboard.writeText(userConnectionString);
      setCopyMessage("Copied to clipboard.");
    } catch {
      setCopyMessage("Copy failed. Select the field and copy manually.");
    }
    if (copyTimer.current != null) window.clearTimeout(copyTimer.current);
    copyTimer.current = window.setTimeout(() => setCopyMessage(null), 2200);
  }, [userConnectionString]);

  const sourceDescriptor = describeEffectiveSource(ai.source, ai.active);
  const portalUrl = appInsightsPortalUrl(config?.subscriptionId, form.component_name, form.resource_group);
  const userValueDiffersFromApplied =
    isWellFormedUserString && userKeyTail !== "" && userKeyTail !== prefs.appInsightsLastAppliedKeyTail;

  return (
    <>
    <Section heading="Telemetry">
      <Group>
        <Row
          label="Send application telemetry to App Insights"
          hint={
            hasConnectionStringSomewhere
              ? "Browser telemetry starts when the toggle is on. Server sidecar updates are explicit (see below)."
              : "Add a connection string or provision an App Insights resource before enabling."
          }
          control={
            <Toggle
              checked={prefs.telemetryEnabled}
              onChange={handleTelemetryToggle}
              label="Application telemetry"
              disabled={!hasConnectionStringSomewhere}
            />
          }
        />
        <Row
          label="Effective source"
          hint={sourceDescriptor.hint}
          control={
            <Badge tone={sourceDescriptor.tone} icon={sourceDescriptor.icon}>
              {sourceDescriptor.label}
            </Badge>
          }
        />
        {(prefs.appInsightsLastAppliedRevision || prefs.appInsightsLastAppliedKeyTail) && (
          <Row
            label="Server sidecars"
            hint={
              prefs.appInsightsLastAppliedKeyTail
                ? `api / worker / beat last received a connection string ending …${prefs.appInsightsLastAppliedKeyTail}.`
                : "api / worker / beat last had the connection string removed."
            }
            control={
              prefs.appInsightsLastAppliedRevision ? (
                <code style={{ fontSize: 11, color: "var(--text-muted)" }}>
                  {prefs.appInsightsLastAppliedRevision}
                </code>
              ) : (
                <Badge tone="muted">No revision</Badge>
              )
            }
          />
        )}
      </Group>

      <Group title="Connection string override">
        <Field
          label="Application Insights connection string"
          hint={
            <>
              Leave blank to use the deployment-provided value. The {" "}
              <a
                href="https://learn.microsoft.com/en-us/azure/azure-monitor/app/sdk-connection-string"
                target="_blank"
                rel="noopener noreferrer"
                style={{ color: "var(--text-muted)", textDecoration: "underline" }}
              >
                connection string
              </a>{" "}
              must include both <code>InstrumentationKey=</code> and <code>IngestionEndpoint=</code>.
              Applying this value updates api, worker, and beat — frontend and terminal are read-only sidecars
              and are intentionally not touched.
            </>
          }
        >
          <div style={{ display: "flex", gap: 6, alignItems: "stretch" }}>
            <input
              type={showSecret ? "text" : "password"}
              value={prefs.appInsightsConnectionString}
              onChange={(event) => setPref("appInsightsConnectionString", event.target.value)}
              placeholder="InstrumentationKey=...;IngestionEndpoint=https://..."
              autoComplete="off"
              spellCheck={false}
              aria-invalid={userConnectionString.length > 0 && !isWellFormedUserString}
              style={{
                ...INPUT_STYLE,
                borderColor:
                  userConnectionString.length === 0
                    ? "var(--border-weak)"
                    : isWellFormedUserString
                      ? "color-mix(in srgb, var(--success) 60%, var(--border-weak))"
                      : "color-mix(in srgb, var(--warning) 60%, var(--border-weak))",
              }}
            />
            <IconButton
              label={showSecret ? "Hide connection string" : "Show connection string"}
              onClick={() => setShowSecret((p) => !p)}
              pressed={showSecret}
            >
              {showSecret ? <EyeOff size={14} /> : <Eye size={14} />}
            </IconButton>
            <IconButton
              label="Copy connection string"
              onClick={copyConnectionString}
              disabled={!userConnectionString}
            >
              <Copy size={14} />
            </IconButton>
          </div>
          {userConnectionString.length > 0 && !isWellFormedUserString && (
            <StatusLine kind="info">
              Waiting for a complete value — the apply button stays disabled until both fields are present.
            </StatusLine>
          )}
          {copyMessage && <StatusLine kind="info">{copyMessage}</StatusLine>}
        </Field>

        <div
          style={{
            display: "flex",
            gap: 8,
            alignItems: "center",
            flexWrap: "wrap",
            paddingBottom: 14,
          }}
        >
          <button
            type="button"
            className="glass-button"
            onClick={sendTest}
            disabled={!ai.active}
            title={ai.active ? "Send a browser pageView to App Insights" : "Enable telemetry and configure a connection string first"}
            style={{ fontSize: 12 }}
          >
            Send test event
          </button>
          <button
            type="button"
            className="glass-button glass-button--primary"
            onClick={applyToDeployment}
            disabled={!isWellFormedUserString || isRunningTask(applyTask) || !userValueDiffersFromApplied}
            title={
              !isWellFormedUserString
                ? "Enter a complete connection string"
                : !userValueDiffersFromApplied
                  ? "Server sidecars already use this connection string"
                  : "Apply this connection string to api, worker, and beat"
            }
            style={{ fontSize: 12, display: "inline-flex", alignItems: "center", gap: 6 }}
          >
            <Upload size={13} strokeWidth={1.5} />
            Apply to server sidecars
          </button>
          <button
            type="button"
            className="glass-button"
            onClick={clearFromDeployment}
            disabled={isRunningTask(clearTask) || !prefs.appInsightsLastAppliedKeyTail}
            title={
              prefs.appInsightsLastAppliedKeyTail
                ? "Remove the connection string from api, worker, and beat"
                : "No server-side override is currently set"
            }
            style={{ fontSize: 12, display: "inline-flex", alignItems: "center", gap: 6 }}
          >
            <Trash2 size={13} strokeWidth={1.5} />
            Clear server override
          </button>
        </div>

        {testMessage && (
          <div style={{ paddingBottom: 8 }}>
            <StatusLine kind={testMessage.kind}>{testMessage.text}</StatusLine>
          </div>
        )}
        {applyError && (
          <div style={{ paddingBottom: 8 }}>
            <StatusLine kind="error">{applyError}</StatusLine>
          </div>
        )}
        {applyTask && (
          <div style={{ paddingBottom: 8 }}>
            <TaskStatusLine task={applyTask} />
          </div>
        )}
        {clearError && (
          <div style={{ paddingBottom: 8 }}>
            <StatusLine kind="error">{clearError}</StatusLine>
          </div>
        )}
        {clearTask && (
          <div style={{ paddingBottom: 8 }}>
            <TaskStatusLine task={clearTask} />
          </div>
        )}
      </Group>

      <Group title="Provision a resource">
        <Row
          label="Create Application Insights"
          hint={
            <>
              Will create <code>{form.workspace_name || "log-…"}</code> (Log Analytics) and {" "}
              <code>{form.component_name || "appi-…"}</code> in {" "}
              <code>{form.resource_group || "<resource group>"}</code> ·{" "}
              <code>{form.region || "<region>"}</code>. Existing resources with the same name are reused.
            </>
          }
          control={
            <button
              type="button"
              className="glass-button"
              onClick={() => setFormOpen((p) => !p)}
              aria-expanded={formOpen}
              style={{ fontSize: 12 }}
            >
              {formOpen ? "Hide form" : "Open form"}
            </button>
          }
        />
        {formOpen && (
          <div style={{ paddingBottom: 14 }}>
            <ProvisionForm
              value={form}
              onChange={setForm}
              onSubmit={provision}
              busy={isRunningTask(task)}
              workloadRegion={config?.region}
              onReset={() => setForm(defaultProvisionForm(config))}
            />
            {error && <StatusLine kind="error">{error}</StatusLine>}
            {task && <TaskStatusLine task={task} />}
          </div>
        )}
        {!formOpen && task?.status === "SUCCESS" && (
          <div style={{ paddingBottom: 14 }}>
            <StatusLine kind="success">
              Provisioning finished. {task.message ?? ""}
            </StatusLine>
          </div>
        )}
        {portalUrl && (
          <div style={{ paddingBottom: 14, display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
            <a
              href={portalUrl}
              target="_blank"
              rel="noopener noreferrer"
              style={{
                fontSize: 12,
                color: "var(--text-muted)",
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                textDecoration: "underline",
              }}
            >
              <ExternalLink size={12} strokeWidth={1.5} /> Open in Azure Portal
            </a>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 11, color: "var(--text-faint)" }}>
              <HelpCircle size={12} strokeWidth={1.5} />
              Connection strings live on the resource Overview blade.
            </span>
          </div>
        )}
      </Group>
    </Section>
    <ConfirmDialog
      open={provisionConfirmOpen}
      title="Provision Application Insights?"
      details={provisionSummaryLines}
      footnote="This creates Azure resources that may incur Log Analytics ingestion charges."
      confirmLabel="Provision"
      confirmAriaLabel="Provision Application Insights"
      tone="primary"
      onConfirm={submitProvision}
      onCancel={() => setProvisionConfirmOpen(false)}
    />
    <ConfirmDialog
      open={clearConfirmOpen}
      title="Remove the App Insights connection string?"
      message="The connection string will be removed from api, worker, and beat."
      footnote="A new Container App revision will roll out. The api / worker / beat sidecars will fall back to whatever the deployment template provides."
      confirmLabel="Remove"
      confirmAriaLabel="Remove connection string from deployment"
      onConfirm={submitClearFromDeployment}
      onCancel={() => setClearConfirmOpen(false)}
    />
    </>
  );
}

function isWellFormedConnectionString(value: string): boolean {
  return value.includes("InstrumentationKey=") && value.includes("IngestionEndpoint=");
}

function extractInstrumentationKeyTail(value: string): string {
  const match = value.match(/InstrumentationKey=([^;]+)/);
  if (!match) return "";
  const key = match[1].trim();
  return key.length > 8 ? key.slice(-8) : key;
}

function describeEffectiveSource(
  source: "user" | "deployment" | "none",
  active: boolean,
): { label: string; hint: string; tone: "success" | "muted" | "warning"; icon: React.ReactNode } {
  if (active && source === "user") {
    return {
      label: "Browser override · active",
      hint: "The SPA is using the connection string entered below.",
      tone: "success",
      icon: <CheckCircle2 size={11} strokeWidth={2} />,
    };
  }
  if (active && source === "deployment") {
    return {
      label: "Deployment · active",
      hint: "Using APPLICATIONINSIGHTS_CONNECTION_STRING injected by the Container App template.",
      tone: "success",
      icon: <CheckCircle2 size={11} strokeWidth={2} />,
    };
  }
  if (source === "user") {
    return {
      label: "Browser override · idle",
      hint: "A connection string is entered but telemetry is disabled.",
      tone: "warning",
      icon: <AlertCircle size={11} strokeWidth={2} />,
    };
  }
  if (source === "deployment") {
    return {
      label: "Deployment · idle",
      hint: "Connection string is available but telemetry is disabled.",
      tone: "warning",
      icon: <AlertCircle size={11} strokeWidth={2} />,
    };
  }
  return {
    label: "Not configured",
    hint: "Enter a connection string below or provision an Application Insights resource.",
    tone: "muted",
    icon: null,
  };
}

function appInsightsPortalUrl(
  subscriptionId: string | undefined | null,
  componentName: string,
  resourceGroup: string,
): string | null {
  if (subscriptionId && resourceGroup && componentName) {
    const path =
      `/subscriptions/${subscriptionId}/resourceGroups/${resourceGroup}` +
      `/providers/Microsoft.Insights/components/${componentName}`;
    return `https://portal.azure.com/#@/resource${path}/overview`;
  }
  return "https://portal.azure.com/#blade/HubsExtension/BrowseResource/resourceType/microsoft.insights%2Fcomponents";
}

function AksSection({ config }: { config: ResourceConfig | null }) {
  const { prefs, setPref } = usePreferences();
  const [clusterName, setClusterName] = useState("");
  const [appInsightsName, setAppInsightsName] = useState("appi-elb-dashboard");
  const [status, setStatus] = useState<string | null>(null);
  const [containerInsightsEnabled, setContainerInsightsEnabled] = useState<boolean | null>(null);
  const [task, setTask] = useState<TaskState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [resolvingWorkspace, setResolvingWorkspace] = useState(false);
  // Track the full cluster objects (not just names) so enable/disable/status
  // requests can forward each cluster's actual `resource_group`. A multi-tier
  // fleet routinely lives across several RGs (e.g. workload RG vs the
  // elastic-blast default RG) and the backend `list_aks_clusters` lookup is
  // RG-scoped, so sending the workspace anchor RG for a cluster that lives
  // elsewhere produces a "cluster not found" failure.
  const [availableClusters, setAvailableClusters] = useState<AksClusterSummary[]>([]);
  const [clustersLoading, setClustersLoading] = useState(false);
  const [clustersLoaded, setClustersLoaded] = useState(false);

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

  // Resolve the selected cluster's *actual* RG. The dropdown stores the
  // cluster name but the backend Observability endpoints need the RG that
  // physically holds the AKS resource (api/services/aks_observability.py
  // calls `client.managed_clusters.get(rg, name)` directly).
  const selectedClusterRg =
    availableClusters.find((c) => c.name === clusterName)?.resource_group ??
    config?.workloadResourceGroup ??
    "";

  const canRead = Boolean(
    config?.subscriptionId && selectedClusterRg && clusterName,
  );

  const refresh = useCallback(async () => {
    if (!config || !canRead) return;
    setError(null);
    try {
      const response = await settingsApi.getAksObservabilityStatus({
        subscription_id: config.subscriptionId,
        resource_group: selectedClusterRg,
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
  }, [canRead, clusterName, config, selectedClusterRg, setPref]);

  useEffect(() => {
    if (!canRead) return;
    void refresh();
  }, [canRead, refresh]);

  useEffect(() => {
    // Sub-wide cluster discovery — matches ClusterCard / StorageCard /
    // BlastSubmit so an ElasticBLAST workload cluster living outside the
    // dashboard anchor RG is still listed in the Observability picker.
    if (!config?.subscriptionId) return;
    let cancelled = false;
    setClustersLoading(true);
    void (async () => {
      try {
        const response = await monitoringApi.aks(config.subscriptionId);
        if (cancelled) return;
        const clusters = (response.clusters ?? []).filter((c) => c.name);
        setAvailableClusters(clusters);
        setClustersLoaded(true);
        setClusterName((current) => {
          if (current && clusters.some((c) => c.name === current)) return current;
          const preferred = pickPreferredCluster(clusters, {
            resourceGroup: config.workloadResourceGroup,
          });
          return preferred?.name ?? current;
        });
      } catch {
        if (cancelled) return;
        setAvailableClusters([]);
        setClustersLoaded(true);
      } finally {
        if (!cancelled) setClustersLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [config?.subscriptionId, config?.workloadResourceGroup]);

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
        resource_group: selectedClusterRg,
        cluster_name: clusterName,
        workspace_resource_id: workspaceId,
      });
      setTask({ taskId: response.task_id, status: "PENDING" });
    } catch (err) {
      setError(formatApiError(err, "aks"));
    }
  }, [clusterName, config, prefs.appInsightsWorkspaceResourceId, resolveWorkspace, selectedClusterRg]);

  const disable = useCallback(async () => {
    if (!config) return;
    setError(null);
    setTask(null);
    try {
      const response = await settingsApi.disableAksObservability({
        subscription_id: config.subscriptionId,
        resource_group: selectedClusterRg,
        cluster_name: clusterName,
      });
      setTask({ taskId: response.task_id, status: "PENDING", message: "Disabling Container Insights" });
    } catch (err) {
      setError(formatApiError(err, "aks"));
    }
  }, [clusterName, config, selectedClusterRg]);

  return (
    <Section heading="AKS Observability">
      <Group>
        <Field
          label="AKS cluster name"
          hint={
            clustersLoading
              ? "Discovering AKS clusters in this subscription..."
              : availableClusters.length > 1
                ? "Pick the cluster whose omsagent addon should be patched."
                : availableClusters.length === 1
                  ? "Container Insights is enabled by patching the omsagent addon on this cluster."
                  : clustersLoaded
                    ? "No ELB-managed AKS clusters were found in this subscription. Create one from the Cluster card first."
                    : "Container Insights is enabled by patching the omsagent addon on this cluster."
          }
        >
          {availableClusters.length > 1 ? (
            <select
              value={clusterName}
              onChange={(event) => setClusterName(event.target.value)}
              style={INPUT_STYLE}
            >
              {availableClusters.map((c) => (
                <option key={`${c.resource_group}/${c.name}`} value={c.name}>
                  {c.name} ({c.power_state ?? "?"})
                </option>
              ))}
            </select>
          ) : (
            <input
              value={clusterName}
              onChange={(event) => setClusterName(event.target.value)}
              placeholder={clustersLoaded && availableClusters.length === 0 ? "No AKS cluster detected" : "aks-..."}
              style={INPUT_STYLE}
            />
          )}
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

/**
 * Public HTTPS endpoint settings — drives `setup_openapi_public_https`
 * / `disable_openapi_public_https`. Installs ingress-nginx + cert-manager
 * on the selected AKS cluster and exposes elb-openapi over an
 * Azure-issued FQDN with a Let's Encrypt cert. Mirrors AksSection's
 * cluster discovery so the dropdown also lists clusters outside the
 * dashboard anchor RG.
 */
function PublicHttpsSection({ config }: { config: ResourceConfig | null }) {
  const [clusterName, setClusterName] = useState("");
  const [availableClusters, setAvailableClusters] = useState<AksClusterSummary[]>([]);
  const [clustersLoading, setClustersLoading] = useState(false);
  const [clustersLoaded, setClustersLoaded] = useState(false);
  const [status, setStatus] = useState<OpenApiPublicHttpsStatus | null>(null);
  const [statusLoading, setStatusLoading] = useState(false);
  const [email, setEmail] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [taskRunning, setTaskRunning] = useState(false);
  const [taskPhase, setTaskPhase] = useState<string>("");
  const pollTimer = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (pollTimer.current !== null) {
        window.clearTimeout(pollTimer.current);
        pollTimer.current = null;
      }
    },
    [],
  );

  const selectedClusterRg =
    availableClusters.find((c) => c.name === clusterName)?.resource_group ??
    config?.workloadResourceGroup ??
    "";

  const subscriptionId = config?.subscriptionId ?? "";
  const canAct = Boolean(subscriptionId && selectedClusterRg && clusterName);

  const refresh = useCallback(async () => {
    setError(null);
    setStatusLoading(true);
    try {
      const data = await aksApi.openApiPublicHttpsStatus();
      setStatus(data);
    } catch (err) {
      setError(formatApiError(err, "aks"));
    } finally {
      setStatusLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    // Mirror AksSection cluster discovery so the picker lists every
    // AKS cluster in the subscription regardless of RG.
    if (!subscriptionId) return;
    let cancelled = false;
    setClustersLoading(true);
    void (async () => {
      try {
        const response = await monitoringApi.aks(subscriptionId);
        if (cancelled) return;
        const clusters = (response.clusters ?? []).filter((c) => c.name);
        setAvailableClusters(clusters);
        setClustersLoaded(true);
        setClusterName((current) => {
          if (current && clusters.some((c) => c.name === current)) return current;
          const preferred = pickPreferredCluster(clusters, {
            resourceGroup: config?.workloadResourceGroup,
          });
          return preferred?.name ?? current;
        });
      } catch {
        if (cancelled) return;
        setAvailableClusters([]);
        setClustersLoaded(true);
      } finally {
        if (!cancelled) setClustersLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [subscriptionId, config?.workloadResourceGroup]);

  // Poll the Celery task until terminal. 3 s cadence matches the original
  // PublicHttpsPanel — install + ACME challenge takes ~3-5 minutes on
  // first run, so we trade a bit of poll volume for a snappier UI flip.
  const pollTask = useCallback(
    (taskId: string) => {
      const tick = async () => {
        try {
          const result = await aksApi.openApiPublicHttpsTaskStatus(taskId);
          const customStatus =
            result.custom_status && typeof result.custom_status === "object"
              ? (result.custom_status as { phase?: string })
              : {};
          const phase = customStatus.phase ?? "";
          if (phase) setTaskPhase(phase);
          const runtime = result.runtime_status ?? "";
          if (runtime === "Completed" || runtime === "Failed" || runtime === "Terminated") {
            setTaskRunning(false);
            // setup_openapi_public_https swallows pipeline errors and
            // returns `{status: 'failed', error: '...'}` as a normal task
            // result, so Celery reports `runtime_status: 'Completed'`
            // even when the actual install failed (e.g. cert-manager
            // webhook never reached Ready). Treat dict-level `status:
            // 'failed'` the same as a runtime-level Failed so the SPA
            // surfaces the error banner instead of silently flipping
            // to the success state.
            const dictFailed = result.output?.status === "failed";
            const taskFailed = runtime !== "Completed" || dictFailed;
            if (!taskFailed) {
              await refresh();
            } else {
              const msg =
                result.output?.error ||
                `Task ${runtime.toLowerCase()} (phase=${phase || "n/a"})`;
              setError(String(msg).slice(0, 600));
            }
            return;
          }
        } catch (err) {
          setError(formatApiError(err, "aks"));
          setTaskRunning(false);
          return;
        }
        pollTimer.current = window.setTimeout(tick, 3_000);
      };
      pollTimer.current = window.setTimeout(tick, 1_500);
    },
    [refresh],
  );

  const enable = async () => {
    if (!canAct) return;
    setError(null);
    setTaskRunning(true);
    setTaskPhase("queued");
    try {
      const res = await aksApi.enableOpenApiPublicHttps(
        subscriptionId,
        selectedClusterRg,
        clusterName,
        email,
      );
      pollTask(res.task_id || res.id);
    } catch (err) {
      setError(formatApiError(err, "aks"));
      setTaskRunning(false);
    }
  };

  const disable = async () => {
    if (!canAct) return;
    setError(null);
    setTaskRunning(true);
    setTaskPhase("queued");
    try {
      const res = await aksApi.disableOpenApiPublicHttps(
        subscriptionId,
        selectedClusterRg,
        clusterName,
      );
      pollTask(res.task_id || res.id);
    } catch (err) {
      setError(formatApiError(err, "aks"));
      setTaskRunning(false);
    }
  };

  const enabled = Boolean(status?.enabled);
  const publicUrl = status?.public_base_url ?? "";

  return (
    <Section heading="Public HTTPS Endpoint">
      <Group>
        <StatusLine kind="info">
          Installs ingress-nginx + cert-manager on the selected AKS cluster and exposes the
          elb-openapi service over an Azure-issued FQDN with a Let&apos;s Encrypt cert.
          First-time install is ~3-5 minutes.
        </StatusLine>
        <Field
          label="AKS cluster"
          hint={
            clustersLoading
              ? "Discovering AKS clusters in this subscription..."
              : availableClusters.length === 0 && clustersLoaded
                ? "No ELB-managed AKS clusters were found in this subscription."
                : "Pick the cluster running elb-openapi."
          }
        >
          {availableClusters.length > 1 ? (
            <select
              value={clusterName}
              onChange={(event) => setClusterName(event.target.value)}
              style={INPUT_STYLE}
            >
              {availableClusters.map((c) => (
                <option key={`${c.resource_group}/${c.name}`} value={c.name}>
                  {c.name} ({c.power_state ?? "?"})
                </option>
              ))}
            </select>
          ) : (
            <input
              value={clusterName}
              onChange={(event) => setClusterName(event.target.value)}
              placeholder={clustersLoaded && availableClusters.length === 0 ? "No AKS cluster detected" : "aks-..."}
              style={INPUT_STYLE}
            />
          )}
        </Field>
        {!enabled && (
          <Field
            label="Operator email (optional)"
            hint="Used by Let's Encrypt to send certificate-expiry notifications."
          >
            <input
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              placeholder="ops@example.com"
              style={INPUT_STYLE}
            />
          </Field>
        )}
        <Row
          label="Status"
          control={
            <Badge tone={enabled ? "success" : "muted"}>
              {statusLoading && !status ? "Checking..." : enabled ? "Exposed" : "Not exposed"}
            </Badge>
          }
        />
        {enabled && publicUrl && (
          <Row
            label="Public endpoint"
            control={
              <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                <code style={{ fontSize: 11, maxWidth: 280, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", display: "inline-block" }}>
                  {publicUrl}
                </code>
                <button
                  type="button"
                  className="glass-button"
                  onClick={() => {
                    if (typeof navigator !== "undefined" && navigator.clipboard) {
                      navigator.clipboard.writeText(publicUrl).catch(() => undefined);
                    }
                  }}
                  title="Copy URL"
                  aria-label="Copy URL"
                  style={{ fontSize: 11 }}
                >
                  <Copy size={12} />
                </button>
                <a
                  href={publicUrl}
                  target="_blank"
                  rel="noreferrer"
                  className="glass-button"
                  style={{ fontSize: 11, textDecoration: "none" }}
                >
                  <ExternalLink size={11} />
                </a>
              </span>
            }
          />
        )}
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", paddingBottom: 14 }}>
          <button
            className="glass-button"
            onClick={() => void refresh()}
            disabled={statusLoading || taskRunning}
            style={{ fontSize: 12 }}
          >
            Refresh status
          </button>
          {enabled ? (
            <button
              className="glass-button"
              onClick={disable}
              disabled={!canAct || taskRunning}
              style={{ fontSize: 12 }}
            >
              Disable
            </button>
          ) : (
            <button
              className="glass-button glass-button--primary"
              onClick={enable}
              disabled={!canAct || taskRunning}
              style={{ fontSize: 12 }}
            >
              Enable
            </button>
          )}
          {taskRunning && (
            <span style={{ fontSize: 11, color: "var(--text-muted)", display: "inline-flex", alignItems: "center", gap: 6 }}>
              <Loader2 size={12} className="spin" /> {taskPhase || "queued"}
            </span>
          )}
        </div>
        {enabled && status && (
          <StatusLine kind="info">
            {[
              status.ingress_lb_ip ? `LB ${status.ingress_lb_ip}` : null,
              status.cert_issuer || null,
              status.cert_expires_at ? `expires ${status.cert_expires_at} (auto-renew)` : null,
              status.updated_at ? `updated ${status.updated_at}` : null,
            ]
              .filter(Boolean)
              .join(" · ")}
          </StatusLine>
        )}
        {error && <StatusLine kind="error">{error}</StatusLine>}
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

function ProvisionForm({
  value,
  onChange,
  onSubmit,
  busy,
  workloadRegion,
  onReset,
}: {
  value: ProvisionFormState;
  onChange: (next: ProvisionFormState) => void;
  onSubmit: () => void;
  busy: boolean;
  workloadRegion?: string;
  onReset: () => void;
}) {
  const update =
    (key: keyof ProvisionFormState) =>
    (event: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
      onChange({
        ...value,
        [key]: key === "retention_days" ? Number(event.target.value) : event.target.value,
      });

  const fieldErrors = useMemo(() => validateProvisionFields(value), [value]);
  const formInvalid = Object.keys(fieldErrors).length > 0;
  const regionMismatch = Boolean(
    workloadRegion && value.region && value.region.trim().toLowerCase() !== workloadRegion.trim().toLowerCase(),
  );
  const datalistId = "elb-azure-regions";
  const idPrefix = "elb-prov";

  const targetHasError = Boolean(
    fieldErrors.subscription_id || fieldErrors.resource_group || fieldErrors.region,
  );
  const targetHasEmpty = !value.subscription_id || !value.resource_group || !value.region;
  const targetForceExpand = targetHasError || targetHasEmpty || regionMismatch;
  const [targetExpanded, setTargetExpanded] = useState(false);
  const showTargetFields = targetExpanded || targetForceExpand;
  const subscriptionTail = value.subscription_id.trim().slice(-12) || "(not set)";
  const targetSummary = `${subscriptionTail} / ${value.resource_group || "(no RG)"} / ${value.region || "(no region)"}`;

  const [lookup, setLookup] = useState<{ state: "idle" | "checking" | "found" | "missing" | "error"; message?: string }>({ state: "idle" });
  const lookupAbort = useRef<AbortController | null>(null);

  // Debounced existence check whenever the component_name / RG / subscription_id are valid.
  useEffect(() => {
    if (
      !SUBSCRIPTION_GUID_RE.test(value.subscription_id) ||
      !RG_NAME_RE.test(value.resource_group) ||
      !RESOURCE_NAME_RE.test(value.component_name)
    ) {
      setLookup({ state: "idle" });
      return;
    }
    const handle = window.setTimeout(async () => {
      lookupAbort.current?.abort();
      const controller = new AbortController();
      lookupAbort.current = controller;
      setLookup({ state: "checking" });
      try {
        await settingsApi.lookupAppInsights({
          subscription_id: value.subscription_id.trim(),
          resource_group: value.resource_group.trim(),
          component_name: value.component_name.trim(),
        });
        if (!controller.signal.aborted) {
          setLookup({ state: "found", message: "An App Insights resource with this name already exists. Provision will reuse it." });
        }
      } catch (err) {
        if (controller.signal.aborted) return;
        const status = (err as { status?: number })?.status;
        if (status === 404) {
          setLookup({ state: "missing", message: "Name is available — Provision will create a new resource." });
        } else if (status === 409) {
          setLookup({ state: "error", message: "Multiple matches in the subscription; pick a different name or set the resource group." });
        } else {
          setLookup({ state: "idle" });
        }
      }
    }, 600);
    return () => {
      window.clearTimeout(handle);
      lookupAbort.current?.abort();
    };
  }, [value.subscription_id, value.resource_group, value.component_name]);

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        if (busy || formInvalid) return;
        onSubmit();
      }}
      style={{ display: "grid", gap: 10, paddingTop: 10 }}
    >
      <fieldset
        disabled={busy}
        style={{ display: "grid", gap: 10, border: 0, padding: 0, margin: 0, minInlineSize: 0 }}
      >
        <div style={{ display: "grid", gap: 6 }}>
          <button
            type="button"
            onClick={() => setTargetExpanded((prev) => !prev)}
            aria-expanded={showTargetFields}
            aria-controls={`${idPrefix}-target-fields`}
            disabled={targetForceExpand}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
              padding: "6px 8px",
              background: "transparent",
              border: "1px solid var(--border-weak)",
              borderRadius: 6,
              color: "var(--text-secondary)",
              fontSize: 12,
              cursor: targetForceExpand ? "default" : "pointer",
              textAlign: "left",
            }}
          >
            {showTargetFields ? <ChevronDown size={12} strokeWidth={1.5} /> : <ChevronRight size={12} strokeWidth={1.5} />}
            <span style={{ color: "var(--text-faint)" }}>Target</span>
            <code style={{ fontSize: 11, color: "var(--text-secondary)" }}>{targetSummary}</code>
            {regionMismatch && (
              <span style={{ marginLeft: "auto", fontSize: 11, color: "var(--warning)" }}>
                region mismatch
              </span>
            )}
            {!regionMismatch && !targetForceExpand && (
              <span style={{ marginLeft: "auto", fontSize: 11, color: "var(--text-faint)" }}>
                {showTargetFields ? "Hide" : "Change"}
              </span>
            )}
          </button>
        </div>

        {showTargetFields && (
        <div id={`${idPrefix}-target-fields`} style={{ display: "grid", gap: 10 }}>
        <ProvisionField
          id={`${idPrefix}-sub`}
          label="Subscription ID"
          hint="36-character GUID. Prefilled from the Setup Wizard."
          error={fieldErrors.subscription_id}
        >
          <input
            id={`${idPrefix}-sub`}
            value={value.subscription_id}
            onChange={update("subscription_id")}
            style={INPUT_STYLE}
            autoComplete="off"
            spellCheck={false}
            required
            aria-required
            aria-invalid={Boolean(fieldErrors.subscription_id)}
            placeholder="00000000-0000-0000-0000-000000000000"
          />
        </ProvisionField>

        <ProvisionField
          id={`${idPrefix}-rg`}
          label="Resource group"
          hint="The Azure Resource Group that will hold the App Insights component."
          error={fieldErrors.resource_group}
        >
          <input
            id={`${idPrefix}-rg`}
            value={value.resource_group}
            onChange={update("resource_group")}
            style={INPUT_STYLE}
            autoComplete="off"
            spellCheck={false}
            required
            aria-required
            aria-invalid={Boolean(fieldErrors.resource_group)}
            placeholder="rg-elb-dashboard"
          />
        </ProvisionField>

        <ProvisionField
          id={`${idPrefix}-region`}
          label="Region"
          hint={
            regionMismatch
              ? `Workload region is ${workloadRegion}. Using a different region for observability adds cross-region latency to Container Insights ingestion.`
              : "Pick a known Azure region or type a custom one."
          }
          error={fieldErrors.region}
          hintTone={regionMismatch ? "warning" : "muted"}
        >
          <input
            id={`${idPrefix}-region`}
            list={datalistId}
            value={value.region}
            onChange={update("region")}
            style={INPUT_STYLE}
            autoComplete="off"
            spellCheck={false}
            required
            aria-required
            aria-invalid={Boolean(fieldErrors.region)}
            placeholder="koreacentral"
          />
          <datalist id={datalistId}>
            {KNOWN_AZURE_REGIONS.map((region) => (
              <option key={region} value={region} />
            ))}
          </datalist>
        </ProvisionField>
        </div>
        )}

        <ProvisionField
          id={`${idPrefix}-name`}
          label="Application Insights name"
          hint="Microsoft CAF prefix is `appi-`. Existing resources with this name are reused."
          error={fieldErrors.component_name}
        >
          <input
            id={`${idPrefix}-name`}
            value={value.component_name}
            onChange={update("component_name")}
            style={INPUT_STYLE}
            autoComplete="off"
            spellCheck={false}
            required
            aria-required
            aria-invalid={Boolean(fieldErrors.component_name)}
            placeholder="appi-elb-dashboard"
          />
          {lookup.state === "checking" && (
            <StatusLine kind="loading">Checking whether the name is already taken…</StatusLine>
          )}
          {lookup.state === "found" && (
            <StatusLine kind="info">{lookup.message}</StatusLine>
          )}
          {lookup.state === "missing" && (
            <StatusLine kind="success">{lookup.message}</StatusLine>
          )}
          {lookup.state === "error" && lookup.message && (
            <StatusLine kind="error">{lookup.message}</StatusLine>
          )}
        </ProvisionField>

        <ProvisionField
          id={`${idPrefix}-ws`}
          label="Log Analytics workspace name"
          hint="Microsoft CAF prefix is `log-`. Workspace-based App Insights requires this."
          error={fieldErrors.workspace_name}
        >
          <input
            id={`${idPrefix}-ws`}
            value={value.workspace_name}
            onChange={update("workspace_name")}
            style={INPUT_STYLE}
            autoComplete="off"
            spellCheck={false}
            required
            aria-required
            aria-invalid={Boolean(fieldErrors.workspace_name)}
            placeholder="log-elb-dashboard"
          />
        </ProvisionField>

        <ProvisionField
          id={`${idPrefix}-ws-rg`}
          label="Workspace resource group (optional)"
          hint="Leave blank to put the workspace in the same resource group as App Insights."
          error={fieldErrors.workspace_resource_group}
        >
          <input
            id={`${idPrefix}-ws-rg`}
            value={value.workspace_resource_group}
            onChange={update("workspace_resource_group")}
            style={INPUT_STYLE}
            autoComplete="off"
            spellCheck={false}
            aria-invalid={Boolean(fieldErrors.workspace_resource_group)}
            placeholder={value.resource_group || "rg-elb-observability"}
          />
        </ProvisionField>

        <ProvisionField
          id={`${idPrefix}-retention`}
          label="Log Analytics retention"
          hint="Workspace data older than this is dropped. PerGB2018 ingestion is the dominant cost — short retention keeps the bill predictable."
        >
          <select
            id={`${idPrefix}-retention`}
            value={value.retention_days}
            onChange={update("retention_days")}
            style={{ ...INPUT_STYLE, fontFamily: "var(--font-mono)" }}
          >
            {RETENTION_DAYS_OPTIONS.map((days) => (
              <option key={days} value={days}>
                {days} days{days === DEFAULT_RETENTION_DAYS ? " (default)" : ""}
              </option>
            ))}
          </select>
        </ProvisionField>

        <StatusLine kind="info">
          Creates 1 Log Analytics workspace (SKU <code>PerGB2018</code>) + 1 workspace-based Application Insights
          component. Both default to Microsoft-managed encryption. Existing resources with the same name are reused
          idempotently.
        </StatusLine>

        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", paddingTop: 6 }}>
          <button
            type="submit"
            className="glass-button glass-button--primary"
            disabled={busy || formInvalid}
            title={formInvalid ? "Fix the highlighted fields first" : "Provision (or reuse) the resources"}
            style={{ display: "inline-flex", alignItems: "center", gap: 6, justifySelf: "start" }}
          >
            {busy ? (
              <Loader2 size={13} style={{ animation: "spin 0.9s linear infinite" }} />
            ) : (
              <Activity size={13} strokeWidth={1.5} />
            )}
            Provision App Insights
          </button>
          <button
            type="button"
            className="glass-button"
            onClick={onReset}
            disabled={busy}
            style={{ fontSize: 12 }}
          >
            Reset to defaults
          </button>
        </div>
      </fieldset>
    </form>
  );
}

function ProvisionField({
  id,
  label,
  hint,
  error,
  hintTone = "muted",
  children,
}: {
  id: string;
  label: React.ReactNode;
  hint?: React.ReactNode;
  error?: string;
  hintTone?: "muted" | "warning";
  children: React.ReactNode;
}) {
  const hintColor = hintTone === "warning" ? "var(--warning)" : "var(--text-faint)";
  return (
    <div style={{ display: "grid", gap: 6 }}>
      <label htmlFor={id} style={{ fontSize: 12, color: "var(--text-muted)" }}>
        {label}
      </label>
      {children}
      {hint && (
        <span style={{ fontSize: 11, color: hintColor, lineHeight: 1.5 }}>{hint}</span>
      )}
      {error && (
        <span style={{ fontSize: 11, color: "var(--danger)", lineHeight: 1.5 }}>{error}</span>
      )}
    </div>
  );
}

function validateProvisionFields(value: ProvisionFormState): Partial<Record<keyof ProvisionFormState, string>> {
  const errors: Partial<Record<keyof ProvisionFormState, string>> = {};
  if (!SUBSCRIPTION_GUID_RE.test(value.subscription_id.trim())) {
    errors.subscription_id = "Must be a 36-character GUID.";
  }
  if (!RG_NAME_RE.test(value.resource_group.trim())) {
    errors.resource_group = "1–90 characters: letters, digits, '-', '.', '_', '(', ')'.";
  }
  if (!RESOURCE_NAME_RE.test(value.component_name.trim())) {
    errors.component_name = "Start with a letter or digit; up to 255 characters.";
  }
  if (!REGION_RE.test(value.region.trim())) {
    errors.region = "Use the lowercase Azure region slug, e.g. 'koreacentral'.";
  }
  if (!RESOURCE_NAME_RE.test(value.workspace_name.trim())) {
    errors.workspace_name = "Start with a letter or digit; up to 255 characters.";
  }
  const wsRg = value.workspace_resource_group.trim();
  if (wsRg && !RG_NAME_RE.test(wsRg)) {
    errors.workspace_resource_group = "1–90 characters: letters, digits, '-', '.', '_', '(', ')'.";
  }
  if (!(RETENTION_DAYS_OPTIONS as readonly number[]).includes(value.retention_days)) {
    errors.retention_days = "Pick a value from the list.";
  }
  return errors;
}

function validateProvisionForm(value: ProvisionFormState): { ok: true } | { ok: false; message: string } {
  const errors = validateProvisionFields(value);
  if (Object.keys(errors).length === 0) return { ok: true };
  const first = Object.values(errors)[0];
  return { ok: false, message: `Fix the highlighted fields first — ${first}` };
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

function Toggle({
  checked,
  onChange,
  label,
  disabled,
  describedBy,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  disabled?: boolean;
  describedBy?: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      aria-describedby={describedBy}
      aria-disabled={disabled || undefined}
      disabled={disabled}
      onClick={() => {
        if (disabled) return;
        onChange(!checked);
      }}
      style={{
        position: "relative",
        width: 36,
        height: 20,
        borderRadius: 999,
        background: checked
          ? "color-mix(in srgb, var(--accent) 30%, var(--bg-tertiary))"
          : "var(--bg-tertiary)",
        border: `1px solid ${checked ? "var(--border-focus)" : "var(--border-medium)"}`,
        cursor: disabled ? "not-allowed" : "pointer",
        padding: 0,
        opacity: disabled ? 0.55 : 1,
      }}
    >
      <span
        style={{
          position: "absolute",
          top: 2,
          left: 2,
          width: 14,
          height: 14,
          borderRadius: "50%",
          background: checked ? "var(--accent)" : "var(--text-muted)",
          transform: checked ? "translateX(16px)" : "translateX(0)",
          transition: "transform 120ms",
        }}
      />
    </button>
  );
}

function IconButton({
  label,
  onClick,
  children,
  pressed,
  disabled,
  title,
}: {
  label: string;
  onClick: () => void;
  children: React.ReactNode;
  pressed?: boolean;
  disabled?: boolean;
  title?: string;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      aria-pressed={typeof pressed === "boolean" ? pressed : undefined}
      title={title ?? label}
      onClick={onClick}
      disabled={disabled}
      style={{
        width: 30,
        height: 30,
        display: "grid",
        placeItems: "center",
        color: disabled ? "var(--text-faint)" : "var(--text-muted)",
        background: "var(--bg-tertiary)",
        border: "1px solid var(--border-weak)",
        borderRadius: 6,
        cursor: disabled ? "not-allowed" : "pointer",
        opacity: disabled ? 0.55 : 1,
      }}
    >
      {children}
    </button>
  );
}

function Badge({ tone, icon, children }: { tone: "success" | "muted" | "warning"; icon?: React.ReactNode; children: React.ReactNode }) {
  const color =
    tone === "success" ? "var(--success)" : tone === "warning" ? "var(--warning)" : "var(--text-faint)";
  const background =
    tone === "success"
      ? "rgba(115,191,105,0.08)"
      : tone === "warning"
        ? "rgba(229,160,55,0.10)"
        : "var(--bg-tertiary)";
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        fontSize: 11,
        textTransform: "uppercase",
        letterSpacing: "0.04em",
        borderRadius: 999,
        padding: "2px 8px",
        border: "1px solid var(--border-weak)",
        color,
        background,
        whiteSpace: "nowrap",
      }}
    >
      {icon}
      {children}
    </span>
  );
}

function StatusLine({ kind, children }: { kind: "info" | "success" | "error" | "loading"; children: React.ReactNode }) {
  const icon = kind === "success" ? <CheckCircle2 size={13} color="var(--success)" /> : kind === "error" ? <AlertCircle size={13} color="var(--danger)" /> : kind === "loading" ? <Loader2 size={13} /> : <Activity size={13} />;
  return <div style={{ display: "flex", alignItems: "flex-start", gap: 8, fontSize: 12, color: "var(--text-muted)", lineHeight: 1.5, marginTop: 4 }}><span style={{ marginTop: 1 }}>{icon}</span><span style={{ wordBreak: "break-word" }}>{children}</span></div>;
}

function TaskStatusLine({ task }: { task: TaskState }) {
  const kind = task.status === "SUCCESS" ? "success" : task.status === "FAILURE" ? "error" : "loading";
  const showProgress =
    task.status !== "SUCCESS" &&
    task.status !== "FAILURE" &&
    typeof task.step === "number" &&
    typeof task.totalSteps === "number" &&
    task.totalSteps > 0;
  return (
    <div>
      <StatusLine kind={kind}>
        Task <code>{task.taskId.slice(0, 8)}...</code> · {task.status}
        {showProgress ? ` · step ${task.step}/${task.totalSteps}` : ""}
        {task.message ? ` — ${task.message}` : ""}
      </StatusLine>
      {showProgress && (
        <div
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={task.totalSteps ?? 0}
          aria-valuenow={task.step ?? 0}
          aria-label="Task progress"
          style={{
            height: 4,
            borderRadius: 999,
            background: "var(--bg-tertiary)",
            overflow: "hidden",
            border: "1px solid var(--border-weak)",
            marginTop: 6,
          }}
        >
          <div
            style={{
              width: `${Math.min(100, Math.round(((task.step ?? 0) / (task.totalSteps ?? 1)) * 100))}%`,
              height: "100%",
              background: "var(--accent)",
              transition: "width 200ms ease-out",
            }}
          />
        </div>
      )}
    </div>
  );
}

function defaultProvisionForm(config: ResourceConfig | null): ProvisionFormState {
  const envFromRg = deriveEnvName(config?.workloadResourceGroup);
  return {
    subscription_id: config?.subscriptionId ?? "",
    resource_group: config?.workloadResourceGroup ?? "",
    component_name: envFromRg ? `appi-${envFromRg}` : "appi-elb-dashboard",
    region: config?.region ?? "koreacentral",
    workspace_name: envFromRg ? `log-${envFromRg}` : "log-elb-dashboard",
    workspace_resource_group: "",
    retention_days: DEFAULT_RETENTION_DAYS,
  };
}

function deriveEnvName(rg: string | undefined | null): string {
  if (!rg) return "";
  // "rg-elb-dashboard" -> "elb-dashboard", "rg-my-app-prod" -> "my-app-prod"
  return rg.replace(/^rg-/i, "").trim();
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
        const progress = status.progress as
          | { message?: string; step?: number; total_steps?: number }
          | undefined;
        setTask({
          taskId: task.taskId,
          status: status.status,
          message: progress?.message ?? status.error,
          step: progress?.step,
          totalSteps: progress?.total_steps,
        });
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
