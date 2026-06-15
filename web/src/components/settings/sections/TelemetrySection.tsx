import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Copy,
  ExternalLink,
  Eye,
  EyeOff,
  HelpCircle,
  Trash2,
  Upload,
} from "lucide-react";

import { formatApiError } from "@/api/client";
import { settingsApi, type AppInsightsProvisionRequest } from "@/api/settings";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import type { ResourceConfig } from "@/components/SetupWizard";
import { Badge, Field, Group, IconButton, Row, Section, StatusLine, Toggle } from "@/components/settings/primitives";
import {
  validateProvisionForm,
  type ProvisionFormState,
} from "@/components/settings/provisionValidation";
import { INPUT_STYLE } from "@/components/settings/styles";
import { isRunningTask, usePollTask, TaskStatusLine, type TaskState } from "@/components/settings/taskState";
import { defaultProvisionForm, ProvisionForm } from "@/components/settings/sections/ProvisionForm";
import { useAppInsights } from "@/hooks/useAppInsights";
import { usePreferences } from "@/hooks/usePreferences";

import {
  appInsightsPortalUrl,
  describeEffectiveSource,
  extractInstrumentationKeyTail,
  isWellFormedConnectionString,
} from "./telemetryHelpers";

/**
 * Telemetry settings section — Application Insights connection-string override
 * and resource provisioning.
 *
 * Extracted verbatim from `SettingsPanel.tsx` (issue #24 SRP split). Owns the
 * browser/deployment telemetry toggle, the server-sidecar apply/clear tasks,
 * and the provision flow (delegating the form to `ProvisionForm`). The pure
 * connection-string / source-badge / portal-link helpers live in
 * `telemetryHelpers.tsx`. Backed by `useAppInsights` / `usePreferences` /
 * `settingsApi`.
 */

export function TelemetrySection({ config }: { config: ResourceConfig | null }) {
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

      {!ai.active && (
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
      )}
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
