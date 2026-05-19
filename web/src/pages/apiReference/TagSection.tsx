import { useState } from "react";
import { ChevronDown, Server } from "lucide-react";

import { TAG_ICONS } from "@/pages/apiReference/constants";
import { EndpointCard } from "@/pages/apiReference/EndpointCard";
import type { OpenApiProxyInfo, SpecEndpoint } from "@/pages/apiReference/types";

export function TagSection({
  tag,
  endpoints,
  baseUrl,
  proxyInfo,
}: {
  tag: { name: string; description?: string };
  endpoints: SpecEndpoint[];
  baseUrl: string;
  proxyInfo?: OpenApiProxyInfo;
}) {
  const [open, setOpen] = useState(true);
  const Icon = TAG_ICONS[tag.name] || Server;

  return (
    <section id={`tag-${tag.name}`}>
      <button
        type="button"
        onClick={() => setOpen((isOpen) => !isOpen)}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 10,
          width: "100%",
          background: "none",
          border: "none",
          cursor: "pointer",
          padding: "10px 0",
          color: "var(--text-primary)",
        }}
      >
        <div
          style={{
            width: 28,
            height: 28,
            borderRadius: 8,
            background: "var(--bg-tertiary)",
            display: "grid",
            placeItems: "center",
          }}
        >
          <Icon size={14} strokeWidth={1.5} style={{ color: "var(--accent)" }} />
        </div>
        <div style={{ flex: 1, textAlign: "left" }}>
          <span style={{ fontSize: 15, fontWeight: 700 }}>{tag.name}</span>
          {tag.description && (
            <span style={{ fontSize: 11, color: "var(--text-faint)", marginLeft: 8 }}>
              {tag.description}
            </span>
          )}
        </div>
        <span
          style={{
            fontSize: 10,
            color: "var(--text-faint)",
            background: "var(--bg-tertiary)",
            padding: "2px 8px",
            borderRadius: 10,
            fontFamily: "var(--font-mono)",
          }}
        >
          {endpoints.length}
        </span>
        <ChevronDown
          size={14}
          style={{
            color: "var(--text-faint)",
            transform: open ? "rotate(0)" : "rotate(-90deg)",
            transition: "transform var(--motion-fast)",
          }}
        />
      </button>
      {open && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 24 }}>
          {endpoints.map((endpoint) => (
            <EndpointCard
              key={`${endpoint.method}-${endpoint.path}`}
              ep={endpoint}
              baseUrl={baseUrl}
              proxyInfo={proxyInfo}
              id={`ep-${endpoint.method}-${endpoint.path.replace(/\//g, "-")}`}
            />
          ))}
        </div>
      )}
    </section>
  );
}