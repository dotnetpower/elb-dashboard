import { useEffect, useMemo, useRef, useState } from "react";
import { Activity, ChevronDown, ChevronRight, Loader2 } from "lucide-react";

import { settingsApi } from "@/api/settings";
import type { ResourceConfig } from "@/components/SetupWizard";
import { StatusLine } from "@/components/settings/primitives";
import { INPUT_STYLE } from "@/components/settings/styles";
import {
  canLookupComponent,
  validateProvisionFields,
  DEFAULT_RETENTION_DAYS,
  KNOWN_AZURE_REGIONS,
  RETENTION_DAYS_OPTIONS,
  type ProvisionFormState,
} from "@/components/settings/provisionValidation";

/**
 * App Insights provisioning form + its field primitive and default factory.
 *
 * Extracted verbatim from `SettingsPanel.tsx` (issue #24 SRP split). Owns the
 * collapsible target block, the debounced name-existence lookup, and per-field
 * validation rendering. Consumed by `TelemetrySection`. Pure form component —
 * the submit/provision side effects stay in the section that owns the task.
 */

export function deriveEnvName(rg: string | undefined | null): string {
  if (!rg) return "";
  // "rg-elb-dashboard" -> "elb-dashboard", "rg-my-app-prod" -> "my-app-prod"
  return rg.replace(/^rg-/i, "").trim();
}

export function defaultProvisionForm(config: ResourceConfig | null): ProvisionFormState {
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

export function ProvisionForm({
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
      !canLookupComponent({
        subscription_id: value.subscription_id,
        resource_group: value.resource_group,
        component_name: value.component_name,
      })
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
