/**
 * ServiceBusSection — Settings panel section for the optional Service Bus
 * BLAST integration.
 *
 * Lets an operator enable/disable the integration, choose the auth mode
 * (Entra RBAC / SAS connection string), point at a namespace + queue + topic,
 * run a non-destructive connection test, see live message counts, edit the
 * dead-letter cleanup policy, and trigger manual purges (behind a confirm).
 *
 * Read-only-friendly: a fetch failure or "no Manage claim" degrades to a
 * status line, never a blank section. No secret material is ever shown — the
 * SAS connection string lives in a Key Vault secret referenced by name only.
 */
import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, Check, Copy, Loader2, Plug, RefreshCw, Trash2 } from "lucide-react";

import { formatApiError } from "@/api/client";
import {
  settingsApi,
  type ServiceBusAuthMode,
  type ServiceBusConfig,
  type ServiceBusCounts,
} from "@/api/settings";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { type ResourceConfig } from "@/components/SetupWizard";
import {
  Badge,
  Field,
  Group,
  Row,
  Section,
  Segmented,
  StatusLine,
  Toggle,
} from "@/components/settings/primitives";

const inputStyle: React.CSSProperties = {
  width: "100%",
  padding: "7px 10px",
  borderRadius: 6,
  border: "1px solid var(--border-medium)",
  background: "var(--bg-tertiary)",
  color: "var(--text-primary)",
  fontSize: 13,
};

const buttonStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
  padding: "7px 12px",
  borderRadius: 6,
  border: "1px solid var(--border-medium)",
  background: "var(--bg-tertiary)",
  color: "var(--text-primary)",
  fontSize: 12,
  cursor: "pointer",
};

type PurgeTarget = "main" | "dlq" | null;

/**
 * A read-only shell command with a copy-to-clipboard affordance. Used by the
 * env-gate remediation banner so an operator can apply the exact
 * `SERVICEBUS_ENABLED=true` command from the browser without hunting through
 * docs. The deployment master switch lives on the Container App revision (it is
 * NOT a runtime toggle — setting it from the dashboard would force a control
 * plane restart and need extra RBAC), so the durable fix stays a deploy-time
 * command; this just makes that command one click away.
 */
function CopyCommand({ label, command }: { label: string; command: string }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    if (typeof navigator === "undefined" || !navigator.clipboard) return;
    void navigator.clipboard.writeText(command).then(
      () => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1500);
      },
      () => undefined,
    );
  };
  return (
    <div style={{ marginTop: 8 }}>
      <div style={{ fontSize: 11.5, color: "var(--text-muted)", marginBottom: 3 }}>{label}</div>
      <div
        style={{
          display: "flex",
          alignItems: "flex-start",
          gap: 8,
          padding: "6px 8px",
          borderRadius: 6,
          border: "1px solid var(--border-medium)",
          background: "var(--bg-tertiary)",
        }}
      >
        <code
          style={{
            flex: 1,
            fontSize: 11.5,
            lineHeight: 1.5,
            color: "var(--text-primary)",
            wordBreak: "break-all",
            whiteSpace: "pre-wrap",
          }}
        >
          {command}
        </code>
        <button
          type="button"
          onClick={copy}
          title="Copy command"
          aria-label={`Copy command: ${label}`}
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 4,
            flexShrink: 0,
            padding: "3px 7px",
            borderRadius: 5,
            border: "1px solid var(--border-medium)",
            background: "var(--bg-secondary)",
            color: copied ? "var(--success, #7fb37f)" : "var(--text-muted)",
            fontSize: 11,
            cursor: "pointer",
          }}
        >
          {copied ? <Check size={12} /> : <Copy size={12} />}
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
    </div>
  );
}

export function ServiceBusSection({ config }: { config: ResourceConfig | null }) {
  const [cfg, setCfg] = useState<ServiceBusConfig | null>(null);
  const [counts, setCounts] = useState<ServiceBusCounts | null>(null);
  const [effectiveEnabled, setEffectiveEnabled] = useState(false);
  const [envGateEnabled, setEnvGateEnabled] = useState(true);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [purgeTarget, setPurgeTarget] = useState<PurgeTarget>(null);
  const [purgeBusy, setPurgeBusy] = useState(false);
  const [namespaces, setNamespaces] = useState<string[]>([]);
  const [discovering, setDiscovering] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await settingsApi.getServiceBus();
      setCfg(res.config);
      setCounts(res.counts);
      setEffectiveEnabled(res.effective_enabled);
      setEnvGateEnabled(res.env_gate_enabled);
    } catch (err) {
      setError(formatApiError(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const patch = (next: Partial<ServiceBusConfig>) =>
    setCfg((prev) => (prev ? { ...prev, ...next } : prev));

  const handleSave = async () => {
    if (!cfg) return;
    setSaving(true);
    setError(null);
    try {
      const res = await settingsApi.putServiceBus(cfg);
      setCfg(res.config);
      await load();
    } catch (err) {
      setError(formatApiError(err));
    } finally {
      setSaving(false);
    }
  };

  const handleDiscover = async () => {
    const subscriptionId = config?.subscriptionId?.trim();
    if (!subscriptionId) {
      setError("Select a subscription in the workspace setup first to discover namespaces.");
      return;
    }
    setDiscovering(true);
    setError(null);
    try {
      const res = await settingsApi.discoverServiceBus({ subscription_id: subscriptionId });
      setNamespaces((res.namespaces ?? []).map((n) => n.fqdn).filter(Boolean));
    } catch (err) {
      setError(formatApiError(err));
    } finally {
      setDiscovering(false);
    }
  };

  const handleTest = async () => {
    if (!cfg) return;
    setTesting(true);
    setTestResult(null);
    try {
      const res = await settingsApi.testServiceBus(cfg);
      setTestResult(
        res.reachable
          ? `Reachable (peeked ${res.peeked ?? 0} message(s), auth=${res.auth_mode}).`
          : `Not reachable: ${res.reason}${res.detail ? ` — ${res.detail}` : ""}`,
      );
    } catch (err) {
      setTestResult(formatApiError(err));
    } finally {
      setTesting(false);
    }
  };

  const handlePurge = async () => {
    if (!purgeTarget) return;
    setPurgeBusy(true);
    try {
      await settingsApi.purgeServiceBus({ dead_letter: purgeTarget === "dlq" });
      setPurgeTarget(null);
      await load();
    } catch (err) {
      setError(formatApiError(err));
    } finally {
      setPurgeBusy(false);
    }
  };

  if (loading && !cfg) {
    return (
      <Section heading="Service Bus integration">
        <StatusLine kind="loading">Loading configuration…</StatusLine>
      </Section>
    );
  }
  if (!cfg) {
    return (
      <Section heading="Service Bus integration">
        <StatusLine kind="error">{error ?? "Configuration unavailable."}</StatusLine>
      </Section>
    );
  }

  const dlq = counts?.queue?.dead_letter_message_count ?? null;
  const active = counts?.queue?.active_message_count ?? null;

  return (
    <Section heading="Service Bus integration">
      {cfg.enabled && !effectiveEnabled && (
        <div
          role="status"
          style={{
            display: "flex",
            gap: 10,
            alignItems: "flex-start",
            padding: "10px 12px",
            marginBottom: 12,
            borderRadius: 8,
            border: "1px solid var(--warning-border, rgba(240,198,116,0.4))",
            background: "var(--warning-surface, rgba(240,198,116,0.08))",
            fontSize: 12.5,
            lineHeight: 1.5,
            color: "var(--text-muted)",
          }}
        >
          <AlertTriangle
            size={15}
            strokeWidth={1.5}
            style={{ flexShrink: 0, marginTop: 1, color: "var(--warning)" }}
          />
          <div>
            <strong style={{ color: "var(--text-primary)" }}>
              Enabled in settings, but not active yet.
            </strong>{" "}
            {!envGateEnabled ? (
              <>
                The deployment master switch <code>SERVICEBUS_ENABLED</code> is
                OFF, so the integration stays dormant and does not appear on the
                dashboard. This switch lives on the Container App revision (a
                deploy-time gate, by design separate from this runtime config —
                both must be ON), so it cannot be flipped from the dashboard
                without restarting the control plane. Apply one of the commands
                below; the integration goes live within ~1 minute.
                <div style={{ marginTop: 6, marginBottom: 2 }}>
                  <CopyCommand
                    label="Durable — survives every redeploy (recommended):"
                    command="azd env set SERVICEBUS_ENABLED true && azd deploy"
                  />
                  <CopyCommand
                    label="Fast — no redeploy, sets the gate on the api/worker/beat sidecars now:"
                    command={
                      "for c in api worker beat; do az containerapp update " +
                      "-n <control-plane-app> -g <control-plane-rg> " +
                      "--container-name $c --set-env-vars SERVICEBUS_ENABLED=true; done"
                    }
                  />
                </div>
              </>
            ) : !cfg.namespace_fqdn ? (
              <>
                No namespace is configured yet. Set the{" "}
                <strong>Namespace FQDN</strong> below and save — the integration
                only goes live once a namespace is attached.
              </>
            ) : (
              <>
                The integration is not reporting as live. Save the configuration,
                then use <strong>Test connection</strong> to confirm the
                namespace is reachable.
              </>
            )}
          </div>
        </div>
      )}
      <Group title="Integration">
        <Row
          label="Enable Service Bus"
          hint="Route every BLAST submission through a Service Bus queue and publish completion events to a topic. Disabled by default."
          control={
            <Toggle
              checked={cfg.enabled}
              onChange={(v) => patch({ enabled: v })}
              label="Enable Service Bus integration"
            />
          }
        />
        <Row
          label="Authentication"
          hint="Entra RBAC (same-tenant namespace, no secrets) or SAS connection string (external/cross-tenant)."
          control={
            <Segmented<ServiceBusAuthMode>
              ariaLabel="Service Bus auth mode"
              value={cfg.auth_mode}
              onChange={(v) => patch({ auth_mode: v })}
              options={[
                { value: "entra", label: "Entra RBAC" },
                { value: "sas", label: "SAS" },
              ]}
            />
          }
        />
      </Group>

      <Group title="Namespace & entities">
        <Field label="Namespace FQDN" hint="e.g. sb-elb-dashboard-krc.servicebus.windows.net">
          <input
            style={inputStyle}
            list="sb-namespace-options"
            value={cfg.namespace_fqdn}
            placeholder="<name>.servicebus.windows.net"
            onChange={(e) => patch({ namespace_fqdn: e.target.value.trim() })}
          />
          <datalist id="sb-namespace-options">
            {namespaces.map((ns) => (
              <option key={ns} value={ns} />
            ))}
          </datalist>
          <button
            type="button"
            style={{ ...buttonStyle, marginTop: 6 }}
            onClick={handleDiscover}
            disabled={discovering}
          >
            {discovering ? <Loader2 size={13} /> : <RefreshCw size={13} />} Discover namespaces
          </button>
        </Field>
        <Field label="Request queue">
          <input
            style={inputStyle}
            value={cfg.request_queue}
            onChange={(e) => patch({ request_queue: e.target.value.trim() })}
          />
        </Field>
        <Field label="Completion topic" hint="Leave blank to run request-only (no completion events).">
          <input
            style={inputStyle}
            value={cfg.completion_topic}
            onChange={(e) => patch({ completion_topic: e.target.value.trim() })}
          />
        </Field>
        {cfg.auth_mode === "sas" && (
          <Field
            label="SAS secret name (Key Vault)"
            hint="The Key Vault secret NAME holding the connection string. The secret value is never shown or stored here."
          >
            <input
              style={inputStyle}
              value={cfg.sas_secret_name}
              onChange={(e) => patch({ sas_secret_name: e.target.value.trim() })}
            />
          </Field>
        )}
        <div style={{ display: "flex", gap: 8, padding: "10px 0" }}>
          <button style={buttonStyle} onClick={handleTest} disabled={testing || !cfg.namespace_fqdn}>
            {testing ? <Loader2 size={13} /> : <Plug size={13} />} Test connection
          </button>
          <button style={buttonStyle} onClick={() => void load()} disabled={loading}>
            <RefreshCw size={13} /> Refresh
          </button>
        </div>
        {testResult && <StatusLine kind="info">{testResult}</StatusLine>}
      </Group>

      <Group title="Runtime">
        <Row
          label="Effective state"
          hint="Both the deployment env switch (SERVICEBUS_ENABLED) and this saved config must be on."
          control={
            <Badge tone={counts?.available ? "success" : "muted"}>
              {counts?.available ? "Live" : (counts?.reason ?? "idle")}
            </Badge>
          }
        />
        {counts?.available && (
          <Row
            label="Queue messages"
            control={
              <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
                active {active ?? "—"} · dead-letter {dlq ?? "—"}
              </span>
            }
          />
        )}
      </Group>

      <Group title="Dead-letter cleanup">
        <Row
          label="Auto-purge dead-letter queue"
          hint="Off by default. Backs up every message to an audit blob before deleting — there is no permanent-delete in the automatic path."
          control={
            <Toggle
              checked={cfg.dlq_cleanup_enabled}
              onChange={(v) => patch({ dlq_cleanup_enabled: v })}
              label="Enable DLQ auto-purge"
            />
          }
        />
        {cfg.dlq_cleanup_enabled && (
          <>
            <Field label="Purge when older than (days)">
              <input
                type="number"
                min={1}
                style={inputStyle}
                value={cfg.dlq_max_age_days}
                onChange={(e) => patch({ dlq_max_age_days: Number(e.target.value) || 1 })}
              />
            </Field>
            <Field label="…or when count exceeds">
              <input
                type="number"
                min={1}
                style={inputStyle}
                value={cfg.dlq_max_count}
                onChange={(e) => patch({ dlq_max_count: Number(e.target.value) || 1 })}
              />
            </Field>
            <Field label="Max messages per cleanup run" hint="Bounded so a backlog drains over several ticks.">
              <input
                type="number"
                min={1}
                style={inputStyle}
                value={cfg.dlq_cleanup_batch}
                onChange={(e) => patch({ dlq_cleanup_batch: Number(e.target.value) || 1 })}
              />
            </Field>
          </>
        )}
      </Group>

      <Group title="Manual actions">
        <Row
          label="Purge dead-letter queue"
          hint="Backs up to the audit blob, then deletes. Confirmation required."
          control={
            <button
              style={{ ...buttonStyle, color: "var(--warning)" }}
              onClick={() => setPurgeTarget("dlq")}
            >
              <Trash2 size={13} /> Purge DLQ
            </button>
          }
        />
        <Row
          label="Purge main queue"
          hint="Discards un-processed requests. Confirmation required."
          control={
            <button
              style={{ ...buttonStyle, color: "var(--danger)" }}
              onClick={() => setPurgeTarget("main")}
            >
              <Trash2 size={13} /> Purge queue
            </button>
          }
        />
      </Group>

      <div style={{ display: "flex", justifyContent: "flex-end", paddingTop: 8 }}>
        <button
          style={{ ...buttonStyle, background: "var(--accent)", color: "#fff", border: "none" }}
          onClick={handleSave}
          disabled={saving}
        >
          {saving ? <Loader2 size={13} /> : null} Save
        </button>
      </div>

      {error && <StatusLine kind="error">{error}</StatusLine>}

      <ConfirmDialog
        open={purgeTarget !== null}
        title={purgeTarget === "dlq" ? "Purge dead-letter queue?" : "Purge main queue?"}
        message={
          purgeTarget === "dlq"
            ? "Dead-letter messages will be backed up to the audit blob and then deleted."
            : "Un-processed request messages will be permanently discarded."
        }
        confirmLabel={purgeBusy ? "Purging…" : "Purge"}
        onConfirm={handlePurge}
        onCancel={() => setPurgeTarget(null)}
      />
    </Section>
  );
}
