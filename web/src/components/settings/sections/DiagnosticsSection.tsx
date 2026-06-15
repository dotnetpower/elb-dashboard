import { useCallback } from "react";
import { useNavigate } from "react-router-dom";
import {
  Activity,
  ChevronRight,
  Gauge,
  HeartPulse,
  Network,
  ShieldCheck,
} from "lucide-react";
import type { ReactNode } from "react";

import { type ResourceConfig } from "@/components/SetupWizard";
import { Section } from "@/components/settings/primitives";

// The "Identity and Security" detail view + its cards were extracted to a
// sibling module (issue #24). Re-exported here so the existing
// `@/components/settings/sections/DiagnosticsSection` import path used by the
// dedicated diagnostics page keeps working.
export { IdentitySecurityDetail } from "./IdentitySecurityDetail";

/**
 * "Diagnose & solve problems" — Azure-portal-style launcher that lists
 * diagnostic categories as cards. Because the Settings slide-over is too narrow
 * for per-resource findings, clicking a category closes the panel and navigates
 * to the dedicated `/diagnostics/:category` page. Identity and Security, plus
 * the Reliability and Availability best-practice checks, all live on that page.
 */
type DiagnosticCategoryId =
  | "identity"
  | "connectivity"
  | "reliability"
  | "availability"
  | "security"
  | "operational";

interface DiagnosticCategory {
  id: DiagnosticCategoryId;
  label: string;
  description: string;
  icon: ReactNode;
  available: boolean;
}

const DIAGNOSTIC_CATEGORIES: DiagnosticCategory[] = [
  {
    id: "identity",
    label: "Identity and Security",
    description:
      "Signed-in account and your effective Azure RBAC role assignments per resource group.",
    icon: <ShieldCheck size={18} strokeWidth={1.5} />,
    available: true,
  },
  {
    id: "operational",
    label: "Operational health",
    description:
      "Live production-incident tracking — Kubernetes warning events, pod restarts / crash loops, node pressure, failed Jobs, failed/stuck BLAST searches, and API route errors, each traceable to the failing object.",
    icon: <HeartPulse size={18} strokeWidth={1.5} />,
    available: true,
  },
  {
    id: "reliability",
    label: "Reliability",
    description:
      "AKS health, autoscaling, availability zones, Kubernetes version, Storage redundancy / data protection, and registry SKU against Well-Architected best practices.",
    icon: <Activity size={18} strokeWidth={1.5} />,
    available: true,
  },
  {
    id: "availability",
    label: "Availability and Performance",
    description:
      "AKS node pressure, network plugin / load balancer SKU, monitoring, sidecar headroom, and API latency / error-rate against Well-Architected best practices.",
    icon: <Gauge size={18} strokeWidth={1.5} />,
    available: true,
  },
  {
    id: "security",
    label: "Security posture",
    description:
      "AKS / Storage / ACR resource hardening against the Well-Architected Security pillar — Entra integration, network access, TLS, shared-key, anonymous access, and encryption.",
    icon: <ShieldCheck size={18} strokeWidth={1.5} />,
    available: true,
  },
  {
    id: "connectivity",
    label: "Connectivity Issues",
    description:
      "Private endpoints, VNet peering, and storage network access reachability checks.",
    icon: <Network size={18} strokeWidth={1.5} />,
    available: false,
  },
];

export function DiagnosticsSection({
  config,
  onClose,
}: {
  config: ResourceConfig | null;
  onClose?: () => void;
}) {
  const navigate = useNavigate();
  void config; // config is read by the dedicated page, not the launcher.

  const open = useCallback(
    (id: DiagnosticCategoryId) => {
      onClose?.();
      navigate(`/diagnostics/${id}`);
    },
    [navigate, onClose],
  );

  return (
    <Section heading="Diagnose & solve problems">
      <div style={{ fontSize: 12, color: "var(--text-faint)", lineHeight: 1.6, margin: "-4px 0 14px" }}>
        Pick a category to investigate. Each opens a focused diagnostic page —
        more categories will be added over time.
      </div>
      <div style={{ display: "grid", gap: 10 }}>
        {DIAGNOSTIC_CATEGORIES.map((cat) => (
          <DiagnosticCategoryCard key={cat.id} category={cat} onOpen={() => open(cat.id)} />
        ))}
      </div>
    </Section>
  );
}

function DiagnosticCategoryCard({
  category,
  onOpen,
}: {
  category: DiagnosticCategory;
  onOpen: () => void;
}) {
  const { available } = category;
  return (
    <button
      type="button"
      onClick={available ? onOpen : undefined}
      disabled={!available}
      aria-disabled={!available}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 14,
        width: "100%",
        textAlign: "left",
        padding: "14px 16px",
        borderRadius: 8,
        border: "1px solid var(--border-weak)",
        background: "var(--bg-secondary)",
        cursor: available ? "pointer" : "default",
        opacity: available ? 1 : 0.6,
        transition: "background 150ms ease, border-color 150ms ease",
      }}
    >
      <span
        aria-hidden
        style={{
          width: 36,
          height: 36,
          borderRadius: 8,
          background: "var(--bg-tertiary)",
          border: "1px solid var(--border-weak)",
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          color: "var(--text-muted)",
          flexShrink: 0,
        }}
      >
        {category.icon}
      </span>
      <span style={{ minWidth: 0, flex: 1 }}>
        <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)" }}>
            {category.label}
          </span>
          {!available && (
            <span
              style={{
                fontSize: 10,
                padding: "1px 7px",
                borderRadius: 999,
                border: "1px solid var(--border-weak)",
                color: "var(--text-faint)",
                whiteSpace: "nowrap",
              }}
            >
              Coming soon
            </span>
          )}
        </span>
        <span style={{ display: "block", fontSize: 11, color: "var(--text-faint)", marginTop: 2, lineHeight: 1.5 }}>
          {category.description}
        </span>
      </span>
      {available && (
        <ChevronRight size={16} strokeWidth={1.5} style={{ color: "var(--text-faint)", flexShrink: 0 }} />
      )}
    </button>
  );
}
