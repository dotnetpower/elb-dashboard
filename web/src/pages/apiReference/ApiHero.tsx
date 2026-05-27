import type { ReactNode } from "react";
import {
  BookOpen,
  Check,
  Copy,
  ExternalLink,
  Globe,
  Hash,
  Network,
  RefreshCw,
  Server,
  Zap,
} from "lucide-react";

import { useClipboardFeedback } from "@/hooks/useClipboardFeedback";
import type { ParsedSpec } from "@/pages/apiReference/types";

// `baseUrl` is the resolved Service IP (typically the AKS internal
// LoadBalancer at 10.x.x.x). When that IP is RFC1918 / loopback / link-
// local, the browser cannot reach it from the public dashboard origin —
// and even if it could, it would mean a plain-HTTP top-level navigation
// from an HTTPS page. The SPA's own API Reference page is the intended
// surface in that case (it proxies every call through the api sidecar),
// so hide the "Swagger UI" external link when the upstream is private.
function isPrivateOrLoopbackIpv4Host(host: string): boolean {
  const match = host.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
  if (!match) return false;
  const octets = match.slice(1, 5).map((value) => Number(value));
  if (octets.some((value) => Number.isNaN(value) || value < 0 || value > 255)) return false;
  const [a, b] = octets;
  if (a === 10) return true;
  if (a === 127) return true;
  if (a === 169 && b === 254) return true;
  if (a === 172 && b >= 16 && b <= 31) return true;
  if (a === 192 && b === 168) return true;
  return false;
}

function isReachableUpstream(baseUrl: string): boolean {
  try {
    const parsed = new URL(baseUrl);
    return !isPrivateOrLoopbackIpv4Host(parsed.hostname);
  } catch {
    return false;
  }
}

export function ApiHero({
  spec,
  baseUrl,
  publicHttpsUrl,
  onRefresh,
  refreshing,
}: {
  spec: ParsedSpec | null;
  baseUrl: string | null;
  publicHttpsUrl?: string | null;
  onRefresh: () => void;
  refreshing: boolean;
}) {
  const totalEndpoints = spec?.endpoints.length ?? 0;
  const methods = spec ? [...new Set(spec.endpoints.map((endpoint) => endpoint.method))] : [];
  const { copied, copyText } = useClipboardFeedback();
  // Prefer the public HTTPS URL for the Swagger UI link when present —
  // the internal LB IP is unreachable from the browser anyway.
  const swaggerHref = publicHttpsUrl ?? baseUrl;
  const showSwagger = Boolean(swaggerHref && isReachableUpstream(swaggerHref));

  return (
    <div className="mono-header api-hero">

      <div className="api-hero__row" style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 16 }}>
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 6 }}>
            <BookOpen size={18} style={{ color: "var(--accent)" }} />
            <h1
              style={{
                margin: 0,
                fontSize: 20,
                fontWeight: 700,
                letterSpacing: 0,
                color: "var(--text-primary)",
              }}
            >
              ElasticBLAST API Reference
            </h1>
            {spec && (
              <span
                style={{
                  fontSize: 10,
                  padding: "2px 8px",
                  borderRadius: 10,
                  background: "var(--bg-tertiary)",
                  color: "var(--text-faint)",
                  fontFamily: "var(--font-mono)",
                  fontWeight: 600,
                }}
              >
                v{spec.version}
              </span>
            )}
          </div>
          <p style={{ margin: 0, fontSize: 13, color: "var(--text-muted)", lineHeight: 1.5 }}>
            {spec ? spec.description.split("\n")[0] : "ElasticBLAST REST API Documentation"}
          </p>

          {spec && (
            <div className="api-hero__stats" style={{ display: "flex", gap: 16, marginTop: 14 }}>
              <Stat icon={<Hash size={11} />} label="Endpoints" value={totalEndpoints} />
              <Stat icon={<Server size={11} />} label="Groups" value={spec.tags.length} />
              <Stat icon={<Zap size={11} />} label="Methods" value={methods.map((method) => method.toUpperCase()).join(", ")} />
            </div>
          )}
        </div>

        <div className="api-hero__actions" style={{ display: "flex", gap: 8, alignItems: "center", flexShrink: 0, flexWrap: "wrap", justifyContent: "flex-end" }}>
          {baseUrl && (
            <UrlChip
              icon={<Network size={11} />}
              label="Internal LB"
              value={baseUrl}
              copied={copied === "api-hero-internal"}
              onCopy={() => copyText(baseUrl, "api-hero-internal")}
              tone="muted"
            />
          )}
          {publicHttpsUrl && (
            <UrlChip
              icon={<Globe size={11} />}
              label="Public HTTPS"
              value={publicHttpsUrl}
              copied={copied === "api-hero-public"}
              onCopy={() => copyText(publicHttpsUrl, "api-hero-public")}
              tone="success"
            />
          )}
          {showSwagger && swaggerHref && (
            <a
              href={`${swaggerHref}/docs`}
              target="_blank"
              rel="noreferrer"
              className="glass-button api-hero__swagger"
              style={{ fontSize: 11, textDecoration: "none" }}
            >
              <ExternalLink size={11} /> Swagger UI
            </a>
          )}
          {baseUrl && (
            <button
              type="button"
              className="glass-button"
              onClick={onRefresh}
              disabled={refreshing}
              style={{ fontSize: 11 }}
              title="Refresh API spec"
              aria-label="Refresh API spec"
            >
              <RefreshCw size={11} className={refreshing ? "spin" : ""} />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function UrlChip({
  icon,
  label,
  value,
  copied,
  onCopy,
  tone,
}: {
  icon: ReactNode;
  label: string;
  value: string;
  copied: boolean;
  onCopy: () => void;
  tone: "muted" | "success";
}) {
  const accent = tone === "success" ? "var(--success)" : "var(--text-faint)";
  return (
    <button
      type="button"
      onClick={onCopy}
      title={copied ? "Copied" : `Click to copy: ${value}`}
      aria-label={`${label}: ${value}. ${copied ? "Copied" : "Click to copy"}`}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        padding: "3px 8px",
        background: "var(--bg-tertiary)",
        border: "1px solid var(--border-weak)",
        borderRadius: 5,
        fontFamily: "var(--font-mono)",
        fontSize: 10,
        color: "var(--text-faint)",
        cursor: "pointer",
        maxWidth: 360,
        minWidth: 0,
      }}
    >
      <span style={{ color: accent, display: "inline-flex" }}>{icon}</span>
      <span
        style={{
          color: tone === "success" ? "var(--text-primary)" : "var(--text-faint)",
          fontWeight: tone === "success" ? 600 : 500,
        }}
      >
        {label}
      </span>
      <span
        style={{
          color: "var(--text-muted)",
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
          minWidth: 0,
        }}
      >
        {value}
      </span>
      <span style={{ color: copied ? "var(--success)" : "var(--text-faint)", display: "inline-flex" }}>
        {copied ? <Check size={11} /> : <Copy size={11} />}
      </span>
    </button>
  );
}

function Stat({ icon, label, value }: { icon: ReactNode; label: string; value: ReactNode }) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 6,
        padding: "5px 12px",
        borderRadius: 8,
        background: "var(--bg-secondary)",
        border: "1px solid var(--border-weak)",
      }}
    >
      <span style={{ color: "var(--accent)", display: "flex" }}>{icon}</span>
      <span style={{ fontSize: 10, color: "var(--text-faint)" }}>{label}</span>
      <span
        style={{
          fontSize: 12,
          fontWeight: 600,
          color: "var(--text-primary)",
          fontFamily: "var(--font-mono)",
        }}
      >
        {value}
      </span>
    </div>
  );
}