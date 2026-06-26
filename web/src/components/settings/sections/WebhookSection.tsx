/**
 * WebhookSection — Settings panel section for outbound webhook notifications.
 *
 * Lets an operator point terminal-job notifications at a Slack / Teams / Discord
 * incoming webhook. The URL is a secret: only a masked form is ever shown, and
 * the server validates it against an SSRF allowlist on save. A "Send test"
 * button posts a sample message. Delivery itself is gated by
 * WEBHOOK_NOTIFICATIONS_ENABLED on the deployment.
 */
import { useEffect, useState } from "react";
import { Webhook } from "lucide-react";

import { formatApiError } from "@/api/client";
import { webhooksApi, type WebhookConfigPublic } from "@/api/webhooks";
import { useToast } from "@/components/Toast";

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

export function WebhookSection() {
  const { toast } = useToast();
  const [config, setConfig] = useState<WebhookConfigPublic | null>(null);
  const [url, setUrl] = useState("");
  const [enabled, setEnabled] = useState(false);
  const [events, setEvents] = useState("terminal");
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);

  useEffect(() => {
    webhooksApi
      .get()
      .then((c) => {
        setConfig(c);
        setEnabled(c.enabled);
        setEvents(c.events || "terminal");
      })
      .catch(() => {});
  }, []);

  const save = async () => {
    setSaving(true);
    try {
      const c = await webhooksApi.put({ url, enabled, events });
      setConfig(c);
      setUrl("");
      toast("Webhook settings saved.", "success");
    } catch (e) {
      toast(`Save failed: ${formatApiError(e)}`, "error");
    } finally {
      setSaving(false);
    }
  };

  const sendTest = async () => {
    setTesting(true);
    try {
      const r = await webhooksApi.test();
      toast(
        r.delivered ? "Test message delivered." : "Test message was not delivered — check the URL.",
        r.delivered ? "success" : "error",
      );
    } catch (e) {
      toast(`Test failed: ${formatApiError(e)}`, "error");
    } finally {
      setTesting(false);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16, maxWidth: 560 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <Webhook size={16} strokeWidth={1.5} style={{ color: "var(--text-muted)" }} />
        <h3 style={{ margin: 0, fontSize: 15 }}>Webhook notifications</h3>
      </div>
      <p style={{ margin: 0, fontSize: 12.5, color: "var(--text-muted)", lineHeight: 1.5 }}>
        Post a message to a Slack / Teams / Discord incoming webhook when a BLAST job
        finishes. The URL is stored as a secret (shown masked) and validated against an
        allowlist. Delivery also requires <code>WEBHOOK_NOTIFICATIONS_ENABLED</code> on the
        deployment.
      </p>

      {config?.configured && (
        <div style={{ fontSize: 12, color: "var(--text-faint)" }}>
          Current: <code>{config.url_masked}</code>
        </div>
      )}

      <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
          Webhook URL {config?.configured ? "(leave blank to keep current; enter to replace)" : ""}
        </span>
        <input
          type="url"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://hooks.slack.com/services/…"
          style={inputStyle}
        />
      </label>

      <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13 }}>
        <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
        Enabled
      </label>

      <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        <span style={{ fontSize: 12, color: "var(--text-muted)" }}>Notify on</span>
        <select value={events} onChange={(e) => setEvents(e.target.value)} style={inputStyle}>
          <option value="terminal">All terminal jobs (completed / failed / cancelled)</option>
          <option value="failed_only">Failed jobs only</option>
        </select>
      </label>

      <div style={{ display: "flex", gap: 8 }}>
        <button
          type="button"
          onClick={save}
          disabled={saving}
          style={{ ...buttonStyle, color: "var(--accent)" }}
        >
          {saving ? "Saving…" : "Save"}
        </button>
        <button
          type="button"
          onClick={sendTest}
          disabled={testing || !config?.configured}
          style={buttonStyle}
          title={config?.configured ? "Send a test message" : "Save a webhook URL first"}
        >
          {testing ? "Sending…" : "Send test"}
        </button>
      </div>
    </div>
  );
}
