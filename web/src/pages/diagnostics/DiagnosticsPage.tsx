/**
 * DiagnosticsPage — the dedicated `/diagnostics(/:category)` surface.
 *
 * The Settings slide-over is too narrow for per-resource best-practice
 * findings, so the Settings "Diagnose & solve problems" cards launch here. A
 * left category rail switches between Identity and Security (RBAC access
 * review, reused from the Settings component), Reliability, and Availability.
 *
 * Reliability / Availability render the read-only findings from
 * `GET /api/diagnostics/{category}` grouped by resource with a severity rollup.
 * Failures and permission denials surface as `indeterminate` findings plus a
 * page banner — never a silent "all clear".
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  Activity,
  AlertCircle,
  AlertTriangle,
  CheckCircle2,
  ChevronRight,
  ExternalLink,
  Gauge,
  HeartPulse,
  HelpCircle,
  Info,
  Loader2,
  Network,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";

import { formatApiError } from "@/api/client";
import {
  diagnosticsApi,
  type DiagnosticCategory,
  type DiagnosticReport,
  type Finding,
  type Severity,
} from "@/api/diagnostics";
import { loadSavedConfig, type ResourceConfig } from "@/components/SetupWizard";
import { IdentitySecurityDetail } from "@/components/settings/sections/DiagnosticsSection";
import {
  RESOURCE_LABEL,
  groupByResource,
  sortBySeverity,
} from "@/pages/diagnostics/diagnosticsModel";

type CategoryId =
  | "identity"
  | "reliability"
  | "availability"
  | "security"
  | "operational"
  | "connectivity";

const RAIL: { id: CategoryId; label: string; icon: React.ReactNode; available: boolean }[] = [
  { id: "identity", label: "Identity and Security", icon: <ShieldCheck size={16} strokeWidth={1.5} />, available: true },
  { id: "operational", label: "Operational health", icon: <HeartPulse size={16} strokeWidth={1.5} />, available: true },
  { id: "reliability", label: "Reliability", icon: <Activity size={16} strokeWidth={1.5} />, available: true },
  { id: "availability", label: "Availability and Performance", icon: <Gauge size={16} strokeWidth={1.5} />, available: true },
  { id: "security", label: "Security posture", icon: <ShieldCheck size={16} strokeWidth={1.5} />, available: true },
  { id: "connectivity", label: "Connectivity Issues", icon: <Network size={16} strokeWidth={1.5} />, available: false },
];

function isCategoryId(value: string | undefined): value is CategoryId {
  return (
    value === "identity" ||
    value === "reliability" ||
    value === "availability" ||
    value === "security" ||
    value === "operational" ||
    value === "connectivity"
  );
}

export default function DiagnosticsPage() {
  const params = useParams();
  const navigate = useNavigate();
  const active: CategoryId = isCategoryId(params.category) ? params.category : "identity";
  const config = useMemo<ResourceConfig | null>(() => loadSavedConfig(), []);

  return (
    <div style={{ padding: "20px 24px", maxWidth: 1100, margin: "0 auto" }}>
      <header style={{ marginBottom: 18 }}>
        <h1 style={{ fontSize: 18, fontWeight: 600, margin: 0, display: "flex", gap: 8, alignItems: "center" }}>
          <ShieldCheck size={18} strokeWidth={1.5} /> Diagnose &amp; solve problems
        </h1>
        <div style={{ fontSize: 12, color: "var(--text-faint)", marginTop: 4 }}>
          Read-only best-practice checks for the configured Azure resources.
        </div>
      </header>

      <div style={{ display: "grid", gridTemplateColumns: "240px 1fr", gap: 20, alignItems: "start" }}>
        <nav aria-label="Diagnostic categories" style={{ display: "grid", gap: 6 }}>
          {RAIL.map((cat) => {
            const selected = cat.id === active;
            return (
              <button
                key={cat.id}
                type="button"
                onClick={cat.available ? () => navigate(`/diagnostics/${cat.id}`) : undefined}
                disabled={!cat.available}
                aria-current={selected ? "page" : undefined}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 10,
                  width: "100%",
                  textAlign: "left",
                  padding: "10px 12px",
                  borderRadius: 8,
                  border: "1px solid var(--border-weak)",
                  background: selected ? "var(--bg-hover)" : "var(--bg-secondary)",
                  boxShadow: selected ? "inset 2px 0 0 var(--accent)" : "none",
                  color: cat.available ? "var(--text-primary)" : "var(--text-faint)",
                  cursor: cat.available ? "pointer" : "default",
                  opacity: cat.available ? 1 : 0.6,
                  fontSize: 12,
                }}
              >
                {cat.icon}
                <span style={{ flex: 1 }}>{cat.label}</span>
                {!cat.available && <span style={{ fontSize: 10 }}>Coming soon</span>}
              </button>
            );
          })}
        </nav>

        <main style={{ minWidth: 0 }}>
          {active === "identity" && (
            <IdentitySecurityDetail config={config} onBack={() => navigate("/")} />
          )}
          {(active === "reliability" || active === "availability" || active === "security" || active === "operational") && (
            <FindingsView category={active} config={config} />
          )}
          {active === "connectivity" && (
            <div style={{ fontSize: 13, color: "var(--text-faint)", padding: "24px 0" }}>
              Connectivity diagnostics are coming soon.
            </div>
          )}
        </main>
      </div>
    </div>
  );
}

function FindingsView({
  category,
  config,
}: {
  category: DiagnosticCategory;
  config: ResourceConfig | null;
}) {
  const [report, setReport] = useState<DiagnosticReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const subscriptionId = config?.subscriptionId ?? "";
  // Tracks the single in-flight run so a Re-run (or category switch) cancels the
  // previous request — a slow earlier response must never overwrite a newer one.
  const activeSignal = useRef<{ cancelled: boolean } | null>(null);

  const run = useCallback(
    async (fresh: boolean) => {
      if (!subscriptionId) {
        setReport(null);
        return;
      }
      // Cancel any previous in-flight run before starting a new one.
      if (activeSignal.current) activeSignal.current.cancelled = true;
      const signal = { cancelled: false };
      activeSignal.current = signal;
      setLoading(true);
      setError(null);
      try {
        const result = await diagnosticsApi.report(
          category,
          {
            subscriptionId,
            workloadResourceGroup: config?.workloadResourceGroup,
            acrResourceGroup: config?.acrResourceGroup,
            acrName: config?.acrName,
            storageAccountName: config?.storageAccountName,
            region: config?.region,
          },
          fresh,
        );
        if (signal.cancelled) return;
        setReport(result);
      } catch (err) {
        if (signal.cancelled) return;
        setError(formatApiError(err, "diagnostics"));
      } finally {
        if (!signal.cancelled) setLoading(false);
      }
    },
    [category, subscriptionId, config?.workloadResourceGroup, config?.acrResourceGroup, config?.acrName, config?.storageAccountName, config?.region],
  );

  // Run on mount / category change. Clear any previous category's report up
  // front so stale findings never render under the new category's header, and
  // cancel the previous run on unmount / category switch (the `cancelled` flag
  // is the codebase's cancellation idiom — `api.get` takes no AbortSignal).
  useEffect(() => {
    setReport(null);
    void run(false);
    return () => {
      if (activeSignal.current) activeSignal.current.cancelled = true;
    };
  }, [run]);

  const handleRerun = useCallback(() => {
    void run(true);
  }, [run]);

  // Practicality: a category has ~20-60 checks, most of them passing. Lead with
  // the actionable ones (critical / warning / indeterminate) and hide the
  // passing/info rows behind a toggle so green checkmarks never bury a problem.
  const [showPassing, setShowPassing] = useState(false);

  if (!subscriptionId) {
    return (
      <div style={{ fontSize: 13, color: "var(--text-muted)", padding: "24px 0", lineHeight: 1.6 }}>
        Select a subscription in the Setup Wizard first — diagnostics need a
        subscription to inspect the configured resources.
      </div>
    );
  }

  const findings = report?.findings ?? [];
  const ACTIONABLE = new Set<Severity>(["critical", "warning", "indeterminate"]);
  const passingCount = findings.filter((f) => !ACTIONABLE.has(f.severity)).length;
  const visibleFindings = showPassing ? findings : findings.filter((f) => ACTIONABLE.has(f.severity));
  const groups = groupByResource(visibleFindings);

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12 }}>
        <RollupChips rollup={report?.rollup ?? {}} />
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          {passingCount > 0 && (
            <button
              type="button"
              onClick={() => setShowPassing((v) => !v)}
              aria-pressed={showPassing}
              style={{
                background: "none",
                border: "none",
                cursor: "pointer",
                color: "var(--text-faint)",
                fontSize: 11,
                padding: 0,
              }}
            >
              {showPassing ? "Hide" : "Show"} {passingCount} passing
            </button>
          )}
          <button className="glass-button" onClick={handleRerun} disabled={loading}>
            {loading ? <Loader2 size={12} strokeWidth={1.5} className="spin" /> : <RefreshCw size={12} strokeWidth={1.5} />}
            {loading ? "Checking…" : "Re-run"}
          </button>
        </div>
      </div>

      {report?.has_indeterminate && (
        <div
          role="status"
          style={{
            display: "flex",
            gap: 8,
            alignItems: "flex-start",
            padding: "10px 12px",
            marginBottom: 12,
            borderRadius: 8,
            border: "1px solid var(--border-weak)",
            background: "var(--bg-secondary)",
            fontSize: 12,
            color: "var(--text-muted)",
          }}
        >
          <HelpCircle size={14} strokeWidth={1.5} style={{ marginTop: 1, flexShrink: 0 }} />
          <span>
            Some checks could not be verified (permission, network, or timeout).
            Those are shown as <strong>indeterminate</strong>, not as problems —
            re-run with a higher role to confirm them.
          </span>
        </div>
      )}

      {error && (
        <div
          role="alert"
          style={{
            display: "flex",
            gap: 8,
            alignItems: "flex-start",
            padding: "10px 12px",
            marginBottom: 12,
            borderRadius: 8,
            border: "1px solid var(--border-weak)",
            background: "var(--bg-secondary)",
            fontSize: 12,
            color: "var(--text-muted)",
          }}
        >
          <AlertCircle size={14} strokeWidth={1.5} style={{ marginTop: 1, flexShrink: 0 }} />
          <span style={{ wordBreak: "break-word" }}>{error}</span>
        </div>
      )}

      {loading && !report && (
        <div style={{ display: "flex", gap: 8, alignItems: "center", color: "var(--text-faint)", fontSize: 12, padding: "20px 0" }}>
          <Loader2 size={14} strokeWidth={1.5} className="spin" /> Running diagnostics…
        </div>
      )}

      {!loading && !error && findings.length === 0 && (
        <div style={{ fontSize: 12, color: "var(--text-faint)", padding: "20px 0" }}>
          No findings for the configured resources.
        </div>
      )}

      {!loading && !error && findings.length > 0 && visibleFindings.length === 0 && (
        <div
          style={{
            display: "flex",
            gap: 8,
            alignItems: "center",
            fontSize: 13,
            color: "var(--text-muted)",
            padding: "20px 0",
          }}
        >
          <CheckCircle2 size={16} strokeWidth={1.5} style={{ color: "var(--success, #4c9a6a)" }} />
          No issues found — all {passingCount} checks passed.
        </div>
      )}

      <div style={{ display: "grid", gap: 16 }}>
        {groups.map(([kind, list]) => (
          <ResourceGroup key={kind} kind={kind} findings={list} />
        ))}
      </div>
    </div>
  );
}

function ResourceGroup({ kind, findings }: { kind: string; findings: Finding[] }) {
  return (
    <section>
      <h2 style={{ fontSize: 13, fontWeight: 600, margin: "0 0 8px", color: "var(--text-muted)" }}>
        {RESOURCE_LABEL[kind] ?? kind}
      </h2>
      <div style={{ display: "grid", gap: 8 }}>
        {sortBySeverity(findings).map((f) => (
          <FindingCard key={`${f.resource_name}:${f.id}`} finding={f} />
        ))}
      </div>
    </section>
  );
}

const SEVERITY_META: Record<
  Severity,
  { label: string; color: string; icon: React.ReactNode }
> = {
  critical: { label: "Critical", color: "var(--danger, #d9534f)", icon: <AlertCircle size={15} strokeWidth={1.5} /> },
  warning: { label: "Warning", color: "var(--warning, #d9a441)", icon: <AlertTriangle size={15} strokeWidth={1.5} /> },
  indeterminate: { label: "Unverified", color: "var(--text-faint)", icon: <HelpCircle size={15} strokeWidth={1.5} /> },
  info: { label: "Info", color: "var(--accent, #5b8def)", icon: <Info size={15} strokeWidth={1.5} /> },
  ok: { label: "OK", color: "var(--success, #4c9a6a)", icon: <CheckCircle2 size={15} strokeWidth={1.5} /> },
};

function severityMeta(severity: string) {
  return SEVERITY_META[severity as Severity] ?? SEVERITY_META.info;
}

function FindingCard({ finding }: { finding: Finding }) {
  const meta = severityMeta(finding.severity);
  const defaultOpen = finding.severity === "critical" || finding.severity === "warning";
  const [open, setOpen] = useState(defaultOpen);

  return (
    <div
      style={{
        border: "1px solid var(--border-weak)",
        borderRadius: 8,
        background: "var(--bg-secondary)",
        overflow: "hidden",
      }}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 10,
          width: "100%",
          textAlign: "left",
          padding: "10px 12px",
          background: "none",
          border: "none",
          cursor: "pointer",
          color: "var(--text-primary)",
        }}
      >
        <span style={{ color: meta.color, display: "inline-flex", flexShrink: 0 }} aria-hidden>
          {meta.icon}
        </span>
        <span style={{ flex: 1, minWidth: 0 }}>
          <span style={{ fontSize: 13, display: "block" }}>{finding.title}</span>
          <span style={{ fontSize: 11, color: "var(--text-faint)" }}>
            {meta.label}
            {finding.resource_name ? ` · ${finding.resource_name}` : ""}
          </span>
        </span>
        <ChevronRight
          size={15}
          strokeWidth={1.5}
          style={{ color: "var(--text-faint)", transform: open ? "rotate(90deg)" : "none", transition: "transform 150ms ease" }}
        />
      </button>
      {open && (
        <div style={{ padding: "0 12px 12px 34px", fontSize: 12, color: "var(--text-muted)", lineHeight: 1.6 }}>
          <div>{finding.detail}</div>
          {finding.recommendation && (
            <div style={{ marginTop: 6 }}>
              <strong style={{ color: "var(--text-primary)" }}>Recommendation: </strong>
              {finding.recommendation}
            </div>
          )}
          {finding.doc_url && (
            <a
              href={finding.doc_url}
              target="_blank"
              rel="noopener noreferrer"
              style={{ display: "inline-flex", alignItems: "center", gap: 4, marginTop: 8, fontSize: 12 }}
            >
              Best-practice guidance <ExternalLink size={12} strokeWidth={1.5} />
            </a>
          )}
        </div>
      )}
    </div>
  );
}

function RollupChips({ rollup }: { rollup: Record<string, number> }) {
  const order: Severity[] = ["critical", "warning", "indeterminate", "info", "ok"];
  const chips = order.filter((sev) => (rollup[sev] ?? 0) > 0);
  if (chips.length === 0) {
    return <span style={{ fontSize: 12, color: "var(--text-faint)" }}>No findings yet</span>;
  }
  return (
    <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
      {chips.map((sev) => {
        const meta = severityMeta(sev);
        return (
          <span
            key={sev}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 5,
              fontSize: 11,
              padding: "3px 9px",
              borderRadius: 999,
              border: "1px solid var(--border-weak)",
              color: meta.color,
              background: "var(--bg-secondary)",
            }}
          >
            <span aria-hidden style={{ display: "inline-flex" }}>{meta.icon}</span>
            {rollup[sev]} {meta.label}
          </span>
        );
      })}
    </div>
  );
}
