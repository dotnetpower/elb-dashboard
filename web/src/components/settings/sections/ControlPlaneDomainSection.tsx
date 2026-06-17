/**
 * ControlPlaneDomainSection — Settings panel section for the control-plane
 * public custom domain (e.g. `https://dashboard.elasticblast.com`).
 *
 * Persists the domain the operator bound to the dashboard Container App. The
 * ElasticBLAST OpenAPI sibling webhooks back to this URL (`CONTROL_PLANE_URL`);
 * when set, the next OpenAPI deploy injects it instead of the auto-generated
 * `*.azurecontainerapps.io` FQDN. Read-only-friendly: a fetch failure degrades
 * to a status line, never a blank section. No secret material is shown.
 *
 * NOTE: this only configures the URL string OpenAPI uses. Binding the domain to
 * the Container App (custom hostname + managed cert + DNS records) is a separate
 * Azure operation — see docs/operate for the `az containerapp hostname` steps.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Check, Copy, Globe2, Loader2, RefreshCw, Save, Trash2 } from "lucide-react";

import { formatApiError } from "@/api/client";
import {
  settingsApi,
  type ControlPlaneUrlSource,
  type ControlPlaneUrlStatus,
} from "@/api/settings";
import { type ResourceConfig } from "@/components/SetupWizard";
import { Badge, Field, Group, Row, Section, StatusLine } from "@/components/settings/primitives";
import { azureClientId } from "@/config/runtime";
import { useClipboardFeedback } from "@/hooks/useClipboardFeedback";

const codeBlockStyle: React.CSSProperties = {
  margin: 0,
  padding: "10px 12px",
  borderRadius: 6,
  border: "1px solid var(--border-weak)",
  background: "var(--bg-tertiary)",
  color: "var(--text-secondary)",
  fontSize: 11,
  lineHeight: 1.6,
  whiteSpace: "pre",
  overflowX: "auto",
};

/**
 * Build the one-shot `az` command that appends the bound dashboard domain to
 * the MSAL app registration's SPA redirect URIs. Without this the interactive
 * sign-in at the custom domain fails with `AADSTS50011` (redirect URI
 * mismatch). The command reads the current list and only appends when missing,
 * so it is safe to re-run.
 */
function buildRedirectUriCommand(clientId: string, domain: string): string {
  const app = clientId || "<APP_CLIENT_ID>";
  return [
    `# Add ${domain} to the MSAL app's SPA redirect URIs (fixes AADSTS50011 on login).`,
    `# Requires the Application Administrator (or Owner) role on the app registration.`,
    `APP=${app}`,
    `DOMAIN=${domain}`,
    `OID=$(az ad app show --id "$APP" --query id -o tsv)`,
    `CUR=$(az ad app show --id "$APP" --query spa.redirectUris -o json)`,
    `BODY=$(DOMAIN="$DOMAIN" CUR="$CUR" python3 -c 'import json,os;u=json.loads(os.environ["CUR"]);d=os.environ["DOMAIN"];u=u if d in u else u+[d];print(json.dumps({"spa":{"redirectUris":u}}))')`,
    `az rest --method PATCH --url "https://graph.microsoft.com/v1.0/applications/$OID" --headers Content-Type=application/json --body "$BODY"`,
  ].join("\n");
}

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

const SOURCE_LABEL: Record<ControlPlaneUrlSource, string> = {
  env: "Pinned by DASHBOARD_PUBLIC_URL env",
  settings: "Custom domain (this section)",
  container_app: "Container App FQDN (default)",
  none: "Not resolvable",
};

/**
 * Client-side mirror of the backend `normalise_control_plane_url` contract so
 * the Save button is gated before the request leaves the browser. The backend
 * stays authoritative — this is only UX. Returns an error string, or "" when OK.
 */
function validateUrl(raw: string): string {
  const value = raw.trim();
  if (!value) return "Enter a URL.";
  // Reject control characters explicitly — they mirror the backend guard that
  // blocks tab/newline injection into the webhook target.
  if (/[\u0000-\u001f\u007f]/.test(value)) {
    return "URL must not contain control characters.";
  }
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return "Enter a full URL including https://.";
  }
  const isLocalhost = parsed.hostname === "localhost" || parsed.hostname === "127.0.0.1";
  if (parsed.protocol !== "https:" && !(parsed.protocol === "http:" && isLocalhost)) {
    return "URL must use https:// (http is allowed only for localhost).";
  }
  if (parsed.pathname !== "" && parsed.pathname !== "/") {
    return "URL must not include a path.";
  }
  if (parsed.search || parsed.hash) {
    return "URL must not include a query or fragment.";
  }
  if (parsed.username || parsed.password) {
    return "URL must not include credentials.";
  }
  return "";
}

export function ControlPlaneDomainSection({ config }: { config: ResourceConfig | null }) {
  void config;
  const [status, setStatus] = useState<ControlPlaneUrlStatus | null>(null);
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const { copied, copyText } = useClipboardFeedback();

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await settingsApi.getControlPlaneUrl();
      setStatus(res);
      setDraft(res.configured_url);
    } catch (err) {
      setError(formatApiError(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const validationError = draft.trim() ? validateUrl(draft) : "";
  const canSave =
    !saving &&
    !clearing &&
    draft.trim().length > 0 &&
    !validationError &&
    draft.trim().replace(/\/+$/, "") !== (status?.configured_url ?? "");

  const save = useCallback(async () => {
    const err = validateUrl(draft);
    if (err) {
      setError(err);
      return;
    }
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const res = await settingsApi.setControlPlaneUrl(draft.trim());
      setStatus(res);
      setDraft(res.configured_url);
      setNotice("Saved. The next OpenAPI deploy will use this domain.");
    } catch (e) {
      setError(formatApiError(e));
    } finally {
      setSaving(false);
    }
  }, [draft]);

  const clear = useCallback(async () => {
    setClearing(true);
    setError(null);
    setNotice(null);
    try {
      const res = await settingsApi.clearControlPlaneUrl();
      setStatus(res);
      setDraft("");
      setNotice("Cleared. OpenAPI falls back to the Container App FQDN.");
    } catch (e) {
      setError(formatApiError(e));
    } finally {
      setClearing(false);
    }
  }, []);

  const busy = saving || clearing;
  const envPinned = status?.source === "env";
  // The bound custom domain (origin) that browsers actually load. Login must be
  // registered against this exact origin on the app registration.
  const boundDomain = (status?.configured_url || "").replace(/\/+$/, "");
  const redirectCommand = useMemo(
    () => (boundDomain ? buildRedirectUriCommand(azureClientId(), boundDomain) : ""),
    [boundDomain],
  );

  return (
    <Section heading="Control plane domain">
      <Group>
        <StatusLine kind="info">
          Bind a custom domain (e.g. <code>https://dashboard.elasticblast.com</code>) to the
          control plane. The ElasticBLAST OpenAPI service webhooks back to this URL, so the
          next OpenAPI deploy uses it instead of the auto-generated
          <code> *.azurecontainerapps.io</code> FQDN. Binding the domain to the Container App
          (hostname + managed certificate + DNS records) is a separate one-time Azure step,
          and the domain must also be registered as a SPA redirect URI on the app registration
          (command below) so interactive sign-in works.
        </StatusLine>

        {envPinned && (
          <StatusLine kind="info">
            A <code>DASHBOARD_PUBLIC_URL</code> environment value is pinned on the api/worker
            sidecars, so it overrides whatever you save here until it is removed.
          </StatusLine>
        )}

        <Field
          label="Custom domain URL"
          hint={
            validationError
              ? validationError
              : "Origin only — https://host[:port], no path. Saved durably; no redeploy needed."
          }
        >
          <input
            type="url"
            value={draft}
            onChange={(event) => {
              setDraft(event.target.value);
              setError(null);
              setNotice(null);
            }}
            placeholder="https://dashboard.elasticblast.com"
            style={inputStyle}
            spellCheck={false}
            autoCapitalize="none"
          />
        </Field>

        <Row
          label="Effective URL"
          hint={status ? SOURCE_LABEL[status.source] : undefined}
          control={
            <Badge tone={status?.source === "settings" ? "success" : "muted"}>
              {loading && !status
                ? "Checking..."
                : status?.effective_url || "Not configured"}
            </Badge>
          }
        />

        {status?.container_app_url && status.source !== "container_app" && (
          <div style={{ padding: "14px 0", borderBottom: "1px solid var(--border-weak)" }}>
            <div style={{ fontSize: 13, color: "var(--text-primary)", marginBottom: 4 }}>
              Fallback (FQDN)
            </div>
            <code
              style={{
                fontSize: 11,
                color: "var(--text-muted)",
                wordBreak: "break-all",
                lineHeight: 1.5,
              }}
            >
              {status.container_app_url}
            </code>
          </div>
        )}

        {boundDomain && (
          <Field
            label="MSAL redirect URI (required for login)"
            hint="Binding the domain to the Container App is not enough: the same origin must also be a SPA redirect URI on the app registration, or interactive sign-in fails with AADSTS50011. Run this once — it appends the domain only if missing, so it is safe to re-run."
          >
            <div style={{ position: "relative" }}>
              <pre style={codeBlockStyle}>{redirectCommand}</pre>
              <button
                type="button"
                onClick={() => copyText(redirectCommand, "redirect-cmd")}
                title="Copy command"
                aria-label="Copy command"
                style={{
                  ...buttonStyle,
                  position: "absolute",
                  top: 6,
                  right: 6,
                  padding: "4px 8px",
                  fontSize: 11,
                }}
              >
                {copied === "redirect-cmd" ? <Check size={12} /> : <Copy size={12} />}
                {copied === "redirect-cmd" ? "Copied" : "Copy"}
              </button>
            </div>
          </Field>
        )}

        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", paddingTop: 4 }}>
          <button style={buttonStyle} onClick={() => void load()} disabled={loading || busy}>
            {loading ? <Loader2 size={13} className="spin" /> : <RefreshCw size={13} />}
            Refresh
          </button>
          <button
            style={{ ...buttonStyle, borderColor: "var(--accent)" }}
            onClick={() => void save()}
            disabled={!canSave}
            title={validationError || undefined}
          >
            {saving ? <Loader2 size={13} className="spin" /> : <Save size={13} />}
            Save domain
          </button>
          <button
            style={buttonStyle}
            onClick={() => void clear()}
            disabled={busy || !status?.configured_url}
          >
            {clearing ? <Loader2 size={13} className="spin" /> : <Trash2 size={13} />}
            Clear
          </button>
          <Globe2 size={14} style={{ color: "var(--text-faint)", marginLeft: "auto" }} />
        </div>

        {notice && <StatusLine kind="success">{notice}</StatusLine>}
        {error && <StatusLine kind="error">{error}</StatusLine>}
      </Group>
    </Section>
  );
}
