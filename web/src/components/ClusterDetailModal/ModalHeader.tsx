import { X } from "lucide-react";

import type { AksAgentPool } from "@/api/endpoints";

/** Premium modal header — accent gradient, status pill, FQDN, stat cards. */
export function ModalHeader({
  clusterName,
  powerState,
  fqdn,
  agentPools,
  networkPlugin,
  onClose,
}: {
  clusterName: string;
  powerState: string | null;
  fqdn?: string | null;
  agentPools?: AksAgentPool[];
  networkPlugin?: string | null;
  onClose: () => void;
}) {
  return (
    <div
      style={{
        padding: "20px 24px 16px",
        background:
          "linear-gradient(135deg, rgba(110,159,255,0.08) 0%, rgba(184,119,217,0.06) 100%)",
        borderBottom: "1px solid var(--border-weak)",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "flex-start",
          justifyContent: "space-between",
        }}
      >
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <div
              style={{
                width: 36,
                height: 36,
                borderRadius: 10,
                background:
                  "linear-gradient(135deg, var(--accent), var(--purple))",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                boxShadow: "0 4px 12px rgba(110,159,255,0.25)",
              }}
            >
              <span style={{ fontSize: 16 }}>⎈</span>
            </div>
            <div>
              <h3
                style={{
                  margin: 0,
                  fontSize: 18,
                  fontWeight: 700,
                  letterSpacing: "-0.02em",
                }}
              >
                {clusterName}
              </h3>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  marginTop: 2,
                }}
              >
                <span
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 4,
                    fontSize: 11,
                    fontWeight: 600,
                    color:
                      powerState === "Running"
                        ? "var(--success)"
                        : "var(--warning)",
                  }}
                >
                  <span
                    style={{
                      width: 6,
                      height: 6,
                      borderRadius: "50%",
                      background:
                        powerState === "Running"
                          ? "var(--success)"
                          : "var(--warning)",
                      boxShadow:
                        powerState === "Running"
                          ? "0 0 8px var(--success)"
                          : "none",
                      animation:
                        powerState === "Running"
                          ? "blink 1.8s ease-in-out infinite"
                          : "none",
                    }}
                  />
                  {powerState ?? "Unknown"}
                </span>
                {fqdn && (
                  <span className="muted" style={{ fontSize: 10 }}>
                    ·
                  </span>
                )}
                {fqdn && (
                  <code
                    style={{
                      fontSize: 9,
                      color: "var(--text-faint)",
                      background: "rgba(255,255,255,0.04)",
                      padding: "2px 6px",
                      borderRadius: 4,
                    }}
                  >
                    {fqdn}
                  </code>
                )}
              </div>
            </div>
          </div>
        </div>
        <button
          className="glass-button"
          onClick={onClose}
          style={{
            padding: "6px 8px",
            border: "none",
            background: "rgba(255,255,255,0.05)",
          }}
          title="Close (Esc)"
        >
          <X size={16} strokeWidth={1.5} />
        </button>
      </div>

      {/* Stat cards row */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(100px, 1fr))",
          gap: 10,
          marginTop: 16,
        }}
      >
        {[
          {
            label: "Nodes",
            value: agentPools?.[0]?.count ?? "—",
            sub: agentPools?.[0]?.vm_size ?? "",
          },
          { label: "K8s", value: networkPlugin ?? "—", sub: "network" },
          {
            label: "Pools",
            value: String(agentPools?.length ?? 0),
            sub: agentPools?.map((p) => p.name).join(", ") ?? "",
          },
          {
            label: "OS",
            value: agentPools?.[0]?.os_type ?? "—",
            sub: agentPools?.[0]?.mode ?? "",
          },
        ].map((s) => (
          <div
            key={s.label}
            style={{
              padding: "10px 12px",
              borderRadius: 8,
              background: "rgba(255,255,255,0.03)",
              border: "1px solid var(--border-weak)",
            }}
          >
            <div
              className="muted"
              style={{
                fontSize: 9,
                textTransform: "uppercase",
                letterSpacing: "0.06em",
              }}
            >
              {s.label}
            </div>
            <div
              style={{
                fontSize: 16,
                fontWeight: 700,
                marginTop: 2,
                letterSpacing: "-0.02em",
              }}
            >
              {s.value}
            </div>
            <div
              className="muted"
              style={{
                fontSize: 9,
                marginTop: 1,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {s.sub}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
