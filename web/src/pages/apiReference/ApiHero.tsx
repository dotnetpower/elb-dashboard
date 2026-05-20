import type { ReactNode } from "react";
import { BookOpen, ExternalLink, Hash, RefreshCw, Server, Zap } from "lucide-react";

import type { ParsedSpec } from "@/pages/apiReference/types";

export function ApiHero({
  spec,
  baseUrl,
  onRefresh,
  refreshing,
}: {
  spec: ParsedSpec | null;
  baseUrl: string | null;
  onRefresh: () => void;
  refreshing: boolean;
}) {
  const totalEndpoints = spec?.endpoints.length ?? 0;
  const methods = spec ? [...new Set(spec.endpoints.map((endpoint) => endpoint.method))] : [];

  return (
    <div className="mono-header api-hero">

      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 16 }}>
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
            <div style={{ display: "flex", gap: 16, marginTop: 14 }}>
              <Stat icon={<Hash size={11} />} label="Endpoints" value={totalEndpoints} />
              <Stat icon={<Server size={11} />} label="Groups" value={spec.tags.length} />
              <Stat icon={<Zap size={11} />} label="Methods" value={methods.map((method) => method.toUpperCase()).join(", ")} />
            </div>
          )}
        </div>

        <div style={{ display: "flex", gap: 8, alignItems: "center", flexShrink: 0 }}>
          {baseUrl && (
            <>
              <span
                style={{
                  fontSize: 10,
                  fontFamily: "var(--font-mono)",
                  color: "var(--text-faint)",
                  padding: "3px 8px",
                  background: "var(--bg-tertiary)",
                  borderRadius: 5,
                }}
              >
                {baseUrl}
              </span>
              <a
                href={`${baseUrl}/docs`}
                target="_blank"
                rel="noreferrer"
                className="glass-button"
                style={{ fontSize: 11, textDecoration: "none" }}
              >
                <ExternalLink size={11} /> Swagger UI
              </a>
              <button
                type="button"
                className="glass-button"
                onClick={onRefresh}
                disabled={refreshing}
                style={{ fontSize: 11 }}
              >
                <RefreshCw size={11} className={refreshing ? "spin" : ""} />
              </button>
            </>
          )}
        </div>
      </div>
    </div>
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