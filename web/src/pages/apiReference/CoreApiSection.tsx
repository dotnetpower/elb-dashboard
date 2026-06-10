import { useState } from "react";
import { ChevronDown, Cpu, Globe } from "lucide-react";

import { EndpointCard } from "@/pages/apiReference/EndpointCard";
import { buildCoreEndpoints, type CoreApiContext } from "@/pages/apiReference/coreEndpoints";

// Distinct accent for the control-plane section so it reads as a different
// host from the AKS-hosted elb-openapi endpoints below it. Teal sits clearly
// apart from the spec sections' `--accent` (blue) without leaving the calm,
// muted palette.
const CORE_ACCENT = "#33b9b0";
const CORE_ACCENT_SOFT = "rgba(51,185,176,0.12)";
const CORE_ACCENT_BORDER = "rgba(51,185,176,0.35)";

/**
 * Always-on "Core" control-plane section.
 *
 * These endpoints live on the dashboard's own api sidecar (same origin), not
 * on the AKS-hosted elb-openapi service, so they stay callable even while the
 * cluster is stopped. The host banner makes that distinction explicit and the
 * section's teal accent visually separates it from the spec-derived groups.
 */
export function CoreApiSection({
  context,
  originLabel,
}: {
  context: CoreApiContext;
  /** Human label for the same-origin dashboard host (e.g. the page origin). */
  originLabel: string;
}) {
  const [open, setOpen] = useState(true);
  const endpoints = buildCoreEndpoints(context);
  const ready = Boolean(context.resourceGroup && context.clusterName);

  return (
    <section
      id="tag-Core"
      style={{
        border: `1px solid ${CORE_ACCENT_BORDER}`,
        borderRadius: 12,
        background: CORE_ACCENT_SOFT,
        padding: "4px 14px 10px",
      }}
    >
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
            background: CORE_ACCENT_SOFT,
            display: "grid",
            placeItems: "center",
          }}
        >
          <Cpu size={14} strokeWidth={1.5} style={{ color: CORE_ACCENT }} />
        </div>
        <div style={{ flex: 1, textAlign: "left" }}>
          <span style={{ fontSize: 15, fontWeight: 700 }}>Core</span>
          <span
            style={{
              fontSize: 10,
              fontWeight: 700,
              marginLeft: 8,
              padding: "2px 7px",
              borderRadius: 6,
              color: CORE_ACCENT,
              background: CORE_ACCENT_SOFT,
              border: `1px solid ${CORE_ACCENT_BORDER}`,
              textTransform: "uppercase",
              letterSpacing: 0.4,
            }}
          >
            Control plane
          </span>
          <span style={{ fontSize: 11, color: "var(--text-faint)", marginLeft: 8 }}>
            Always-on dashboard API — works even while the cluster is stopped.
          </span>
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
        <>
          {/* Host banner — make the different host explicit. */}
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              margin: "2px 0 12px",
              padding: "8px 12px",
              borderRadius: 8,
              border: `1px solid ${CORE_ACCENT_BORDER}`,
              background: "var(--bg-secondary)",
              fontSize: 11.5,
              color: "var(--text-muted)",
              lineHeight: 1.6,
            }}
          >
            <Globe size={14} style={{ color: CORE_ACCENT, flexShrink: 0 }} />
            <span>
              Different host:&nbsp;
              <code
                style={{
                  color: CORE_ACCENT,
                  fontFamily: "var(--font-mono)",
                  fontSize: 11,
                }}
              >
                {originLabel || "this dashboard"}
              </code>
              &nbsp;(the control-plane api sidecar). The endpoints below are{" "}
              <strong>not</strong> served by the in-cluster{" "}
              <code style={{ fontFamily: "var(--font-mono)", fontSize: 11 }}>
                elb-openapi
              </code>{" "}
              service, so they answer even when the cluster is stopped — that is
              how you wake it.
            </span>
          </div>

          {!ready && (
            <div
              style={{
                margin: "0 0 12px",
                padding: "8px 12px",
                borderRadius: 8,
                border: "1px solid var(--border-weak)",
                background: "var(--bg-secondary)",
                fontSize: 11.5,
                color: "var(--text-faint)",
              }}
            >
              Select a workload Resource Group and cluster on the Dashboard to
              pre-fill the request body.
            </div>
          )}

          <div style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 8 }}>
            {endpoints.map((endpoint) => (
              <EndpointCard
                key={`${endpoint.method}-${endpoint.path}`}
                ep={endpoint}
                baseUrl={originLabel}
                dashboardApi
                id={`ep-core-${endpoint.method}-${endpoint.path.replace(/\//g, "-")}`}
              />
            ))}
          </div>
        </>
      )}
    </section>
  );
}
