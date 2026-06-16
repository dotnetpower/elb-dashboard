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
import { AlertTriangle, Loader2, Plug, RefreshCw, Trash2 } from "lucide-react";

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
                dashboard. Pin <code>SERVICEBUS_ENABLED=true</code> (azd env or a
                GitHub repo variable) and redeploy the control plane to activate
                it. Both the deployment gate and this config must be ON.
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
        <Field
          label="Result queue"
          hint="Completion events are published here. An external service drains this queue to receive BLAST results."
        >
          <input
            style={inputStyle}
            value={cfg.completion_queue}
            onChange={(e) => patch({ completion_queue: e.target.value.trim() })}
          />
        </Field>
        <Field
          label="Future fan-out topic (optional)"
          hint="Off by default — messaging is unified on queues. Enable to ALSO publish completion events to a topic for future fan-out subscribers."
        >
          <Toggle
            checked={cfg.completion_topic_enabled}
            onChange={(v) => patch({ completion_topic_enabled: v })}
            label="Also publish to completion topic"
          />
        </Field>
        {cfg.completion_topic_enabled && (
          <Field label="Completion topic">
            <input
              style={inputStyle}
              value={cfg.completion_topic}
              onChange={(e) => patch({ completion_topic: e.target.value.trim() })}
            />
          </Field>
        )}
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
        {counts?.available && counts?.result_queue && (
          <Row
            label="Result queue messages"
            control={
              <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
                active {counts.result_queue.active_message_count ?? "—"} · dead-letter{" "}
                {counts.result_queue.dead_letter_message_count ?? "—"}
              </span>
            }
          />
        )}
        <Row
          label="Auto-start cluster on request"
          hint="When a BLAST request arrives while the configured AKS cluster is stopped, the control plane auto-starts it before draining. Requires the cluster routing (subscription / resource group / cluster) to be set."
          control={
            <Toggle
              checked={cfg.autostart_cluster_enabled}
              onChange={(v) => patch({ autostart_cluster_enabled: v })}
              label="Enable wake-on-request auto-start"
            />
          }
        />
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
