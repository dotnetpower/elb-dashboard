import { Server } from "lucide-react";

import type { SidecarMetric } from "@/hooks/useSidecarMetrics";

import { HEALTH_LABEL, ICONS, NODE_W } from "./constants";
import { StatusDot } from "./StatusDot";

export interface TopoNodeProps {
  s: SidecarMetric;
  width?: number;
}

export function TopoNode({ s, width = NODE_W }: TopoNodeProps) {
  const cpu = s.cpu_pct ?? null;
  const mem = s.mem_pct ?? null;
  return (
    <div
      style={{
        width,
        padding: "10px 12px",
        borderRadius: 12,
        position: "relative",
        zIndex: 1,
        border: `1px solid ${
          s.health === "ok"
            ? "rgba(106,214,163,0.35)"
            : s.health === "degraded"
              ? "rgba(240,198,116,0.45)"
              : "rgba(224,123,138,0.45)"
        }`,
        background: "var(--bg-tertiary)",
        boxShadow:
          s.health === "ok"
            ? "0 0 16px rgba(106,214,163,0.12)"
            : s.health === "degraded"
              ? "0 0 16px rgba(240,198,116,0.12)"
              : "0 0 16px rgba(224,123,138,0.12)",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <StatusDot health={s.health} size={9} />
        <span style={{ color: "var(--text-faint)", display: "flex" }}>
          {ICONS[s.name] ?? <Server size={14} strokeWidth={1.5} />}
        </span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 13, fontWeight: 600, lineHeight: 1.2 }}>
            {s.name}
          </div>
          <div style={{ fontSize: 10, color: "var(--text-muted)" }}>
            {HEALTH_LABEL[s.health]}
          </div>
        </div>
      </div>
      <div
        style={{
          marginTop: 8,
          fontSize: 10,
          color: "var(--text-faint)",
          display: "flex",
          justifyContent: "space-between",
        }}
      >
        <span>cpu {cpu == null ? "—" : `${cpu}%`}</span>
        <span>mem {mem == null ? "—" : `${mem}%`}</span>
      </div>
    </div>
  );
}
