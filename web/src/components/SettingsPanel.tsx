import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity,
  ArrowUpCircle,
  CheckCircle2,
  ExternalLink,
  Gauge,
  Globe,
  HardDrive,
  Loader2,
  Monitor,
  Network,
  FlaskConical,
  RefreshCw,
  RotateCcw,
  Radio,
  Settings as SettingsIcon,
  Stethoscope,
  Sun,
  X,
} from "lucide-react";
import { Link } from "react-router-dom";

import { formatApiError } from "@/api/client";
import { isCommitUpdateAvailable, githubCompareUrl, upgradeApi } from "@/api/upgrade";
import { useUpgradeAvailability } from "@/hooks/useUpgradeAvailability";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { clearConfig, loadSavedConfig, type ResourceConfig } from "@/components/SetupWizard";
import {
  Badge,
  Group,
  IconButton,
  Row,
  Section,
  StatusLine,
  Toggle,
} from "@/components/settings/primitives";
import {
  AppearanceSection,
  PreviewSection,
} from "@/components/settings/sections/AppearancePreviewSections";
import { SizingSection } from "@/components/settings/sections/SizingSection";
import { PerformanceSection } from "@/components/settings/sections/PerformanceSection";
import { TelemetrySection } from "@/components/settings/sections/TelemetrySection";
import { AksSection } from "@/components/settings/sections/AksSection";
import { PublicHttpsSection } from "@/components/settings/sections/PublicHttpsSection";
import { ServiceBusSection } from "@/components/settings/sections/ServiceBusSection";
import { VnetPeeringSection } from "@/components/settings/sections/VnetPeeringSection";
import { DiagnosticsSection } from "@/components/settings/sections/DiagnosticsSection";
import { useFocusTrap } from "@/hooks/useFocusTrap";
import { usePreferences } from "@/hooks/usePreferences";
import { formatBuildVersion } from "@/utils/buildVersion";

export type SettingsSectionId =
  | "appearance"
  | "preview"
  | "updates"
  | "telemetry"
  | "aks"
  | "performance"
  | "public-https"
  | "vnet-peering"
  | "service-bus"
  | "sizing"
  | "diagnostics"
  | "resources";

type SectionId = SettingsSectionId;

const SECTIONS: Array<{ id: SectionId; label: string; icon: React.ReactNode }> = [
  { id: "appearance", label: "Appearance", icon: <Sun size={14} strokeWidth={1.5} /> },
  { id: "preview", label: "Preview", icon: <FlaskConical size={14} strokeWidth={1.5} /> },
  { id: "updates", label: "Updates", icon: <ArrowUpCircle size={14} strokeWidth={1.5} /> },
  { id: "telemetry", label: "Telemetry", icon: <Activity size={14} strokeWidth={1.5} /> },
  { id: "aks", label: "AKS Observability", icon: <Monitor size={14} strokeWidth={1.5} /> },
  { id: "performance", label: "Performance", icon: <HardDrive size={14} strokeWidth={1.5} /> },
  { id: "public-https", label: "Public HTTPS", icon: <Globe size={14} strokeWidth={1.5} /> },
  { id: "vnet-peering", label: "VNet peering", icon: <Network size={14} strokeWidth={1.5} /> },
  { id: "service-bus", label: "Service Bus", icon: <Radio size={14} strokeWidth={1.5} /> },
  { id: "sizing", label: "Sizing", icon: <Gauge size={14} strokeWidth={1.5} /> },
  { id: "diagnostics", label: "Diagnose & solve problems", icon: <Stethoscope size={14} strokeWidth={1.5} /> },
  { id: "resources", label: "Resources", icon: <SettingsIcon size={14} strokeWidth={1.5} /> },
];

// Sections whose state lives in `localStorage["elb-prefs"]` and is therefore
// what the footer "Reset" button actually clears. Keep this in sync with the
// sections that consume `usePreferences` / `useTheme` (Appearance, Preview,
// Telemetry).
const PREF_BACKED_SECTIONS = new Set<SectionId>(["appearance", "preview", "telemetry"]);

interface Props {
  open: boolean;
  onClose: () => void;
  /**
   * Section to focus when the panel opens. Lets a deep-link entry point (e.g.
   * the topbar "update available" indicator) land the user directly on the
   * relevant section instead of the default Appearance tab — otherwise the
   * indicator opens Settings on Appearance and the update info looks "missing".
   */
  initialSection?: SectionId;
}

export function SettingsPanel({ open, onClose, initialSection }: Props) {
  const trapRef = useFocusTrap<HTMLDivElement>(open);
  const [active, setActive] = useState<SectionId>(initialSection ?? "appearance");
  const { reset } = usePreferences();
  // Mirror the topbar gear dot inside the panel so the user can see *which*
  // section needs attention. Without this, the gear shows a dot but every
  // section in the left-nav looks identical once the panel is open. The hook
  // is shared (Layout gear, UpdatesSection) and broadcast-synced, so this extra
  // consumer just reads the same status while the panel is mounted.
  const { attention: updateAttention } = useUpgradeAvailability();
  const config = useMemo<ResourceConfig | null>(() => (open ? loadSavedConfig() : null), [open]);
  // The Reset button clears the browser-local preferences in
  // `localStorage["elb-prefs"]` (theme, preview flags, telemetry/connection
  // string). Only show it on the sections that are actually backed by those
  // preferences — on the other sections (Resources, Updates, AKS, …) it would
  // appear to do nothing, which reads as "the button is broken".
  const showFooterActions = PREF_BACKED_SECTIONS.has(active);
  const [resetOpen, setResetOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    const handler = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [open, onClose]);

  // The panel stays mounted (it only returns null while closed), so `active`
  // would otherwise persist the last-viewed section across opens. When a caller
  // requests a specific section, focus it each time the panel opens so a
  // deep-link (e.g. the update indicator → Updates) always lands correctly.
  useEffect(() => {
    if (open && initialSection) setActive(initialSection);
  }, [open, initialSection]);

  if (!open) return null;

  return (
    <>
      <div
        onClick={onClose}
        style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.42)", zIndex: 59 }}
      />
      <aside
        role="dialog"
        aria-modal="true"
        aria-label="Settings"
        ref={trapRef}
        style={{
          position: "fixed",
          top: 0,
          right: 0,
          bottom: 0,
          width: "min(720px, calc(100vw - 24px))",
          background: "var(--bg-primary)",
          borderLeft: "1px solid var(--border-medium)",
          boxShadow: "-12px 0 40px rgba(0,0,0,0.45)",
          zIndex: 60,
          display: "grid",
          gridTemplateRows: "56px 1fr 60px",
        }}
      >
        <header
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            padding: "0 20px",
            borderBottom: "1px solid var(--border-weak)",
          }}
        >
          <h2 style={{ fontSize: 14, fontWeight: 600, margin: 0, display: "flex", gap: 8 }}>
            <SettingsIcon size={16} strokeWidth={1.5} /> Settings
          </h2>
          <IconButton label="Close settings" onClick={onClose}>
            <X size={16} />
          </IconButton>
        </header>

        <div style={{ display: "grid", gridTemplateColumns: "180px 1fr", minHeight: 0 }}>
          <nav
            aria-label="Settings sections"
            style={{ borderRight: "1px solid var(--border-weak)", padding: "12px 8px" }}
          >
            {SECTIONS.map((section) => {
              const selected = section.id === active;
              const needsAttention = section.id === "updates" && updateAttention;
              return (
                <button
                  key={section.id}
                  onClick={() => setActive(section.id)}
                  aria-current={selected ? "page" : undefined}
                  style={{
                    width: "100%",
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                    padding: "8px 12px",
                    borderRadius: 6,
                    border: "none",
                    cursor: "pointer",
                    textAlign: "left",
                    color: selected ? "var(--text-primary)" : "var(--text-muted)",
                    background: selected ? "var(--bg-hover)" : "transparent",
                    boxShadow: selected ? "inset 2px 0 0 var(--accent)" : "none",
                    fontSize: 12,
                  }}
                >
                  {section.icon}
                  {section.label}
                  {needsAttention && (
                    <span
                      aria-label="An update is available"
                      role="img"
                      title="An update is available"
                      style={{
                        marginLeft: "auto",
                        width: 7,
                        height: 7,
                        borderRadius: "50%",
                        background: "var(--warning, #d8a657)",
                        flexShrink: 0,
                      }}
                    />
                  )}
                </button>
              );
            })}
          </nav>

          <main style={{ padding: "20px 24px", overflowY: "auto" }}>
            {active === "appearance" && <AppearanceSection />}
            {active === "preview" && <PreviewSection />}
            {active === "updates" && <UpdatesSection onClose={onClose} />}
            {active === "telemetry" && <TelemetrySection config={config} />}
            {active === "aks" && <AksSection config={config} />}
            {active === "performance" && <PerformanceSection config={config} />}
            {active === "public-https" && <PublicHttpsSection config={config} />}
            {active === "vnet-peering" && <VnetPeeringSection config={config} />}
            {active === "service-bus" && <ServiceBusSection config={config} />}
            {active === "sizing" && <SizingSection />}
            {active === "diagnostics" && <DiagnosticsSection config={config} onClose={onClose} />}
            {active === "resources" && <ResourcesSection config={config} />}
          </main>
        </div>

        <footer
          style={{
            padding: "12px 20px",
            borderTop: "1px solid var(--border-weak)",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 12,
          }}
        >
          <span style={{ color: "var(--text-faint)", fontSize: 11, display: "inline-flex", alignItems: "center", gap: 8 }}>
            <span
              title={`Release: v${__APP_VERSION__}\nBuild: v${formatBuildVersion(__APP_VERSION__, __APP_BUILD_NUMBER__)}\nCommit: ${__APP_COMMIT__}`}
              style={{ color: "var(--text-muted)", fontVariantNumeric: "tabular-nums" }}
            >
              v{formatBuildVersion(__APP_VERSION__, __APP_BUILD_NUMBER__)} · {__APP_COMMIT__}
            </span>
            <span style={{ opacity: 0.5 }}>·</span>
            Stored locally · <code>elb-prefs</code>
          </span>
          {showFooterActions && (
            <div style={{ display: "flex", gap: 8 }}>
              <button
                className="glass-button"
                onClick={() => setResetOpen(true)}
                style={{ fontSize: 12 }}
              >
                Reset
              </button>
            </div>
          )}
        </footer>
      </aside>
      <ConfirmDialog
        open={resetOpen}
        title="Reset preferences?"
        message="Reset theme, preview features, telemetry, and connection string preferences?"
        confirmLabel="Reset"
        confirmAriaLabel="Reset preferences"
        onConfirm={() => { setResetOpen(false); reset(); }}
        onCancel={() => setResetOpen(false)}
      />
    </>
  );
}

function formatCheckedAt(iso: string): string {
  if (!iso) return "";
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return parsed.toLocaleString();
}

/**
 * Server self-upgrade status + an explicit "Check now" control. Replaces
 * the old header badge. Status comes from the shared
 * `useUpgradeAvailability` hook, which polls `/api/upgrade/status` on a 60s
 * (visibility-gated) cadence and fans out across tabs via BroadcastChannel —
 * so an available update surfaces here (and on the Settings gear dot) without
 * a click. The button forces a `/api/upgrade/check`, absorbing the 429
 * throttle. The full start/rollback flow lives on the `/upgrade` page.
 */
function UpdatesSection({ onClose }: { onClose: () => void }) {
  const { status, loading, error, available, phase, checkNow, applyStatus } =
    useUpgradeAvailability();
  const [checking, setChecking] = useState(false);
  const [savingChannel, setSavingChannel] = useState(false);
  const [actionMessage, setActionMessage] = useState<
    { kind: "info" | "success" | "error"; text: string } | null
  >(null);

  const configured = Boolean(status?.git_remote);
  const inProgress = phase === "active";
  const failed = phase === "failed";
  const rolledBack = phase === "rolled_back";
  const trackCommits = status?.track_commits ?? true;
  const commitAvailable = isCommitUpdateAvailable(status, __APP_COMMIT__);
  const releaseAvailable = available && !commitAvailable;
  const updateAvailable = releaseAvailable || commitAvailable;
  const compareUrl = githubCompareUrl(status, __APP_COMMIT__);

  const handleCheck = useCallback(async () => {
    setChecking(true);
    setActionMessage(null);
    try {
      const fresh = await checkNow();
      if (!fresh.git_remote) {
        // The check ran but no upgrade remote is configured, so there is
        // nothing upstream to compare against. Say so plainly instead of a
        // misleading "checked" success.
        setActionMessage({
          kind: "info",
          text: "No upgrade remote is configured — nothing to check.",
        });
      } else {
        // Don't repeat the availability badge here — just confirm the action ran.
        setActionMessage({ kind: "success", text: "Checked just now." });
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      // `/check` is throttled (429) to protect the upstream git remote.
      if (msg.includes("429")) {
        setActionMessage({
          kind: "info",
          text: "Check throttled — try again shortly. Status still refreshes automatically.",
        });
      } else {
        setActionMessage({ kind: "error", text: formatApiError(err) });
      }
    } finally {
      setChecking(false);
    }
  }, [checkNow]);

  const handleToggleChannel = useCallback(
    async (next: boolean) => {
      setSavingChannel(true);
      setActionMessage(null);
      try {
        const fresh = await upgradeApi.setTrackCommits(next);
        applyStatus(fresh);
        // Re-discover with the new channel so the badge reflects it
        // immediately (best-effort — the periodic poll catches up if the
        // check is throttled).
        try {
          await checkNow();
        } catch {
          /* throttled / transient — ignore */
        }
        setActionMessage({
          kind: "success",
          text: next
            ? "Tracking new commits (preview) and releases."
            : "Tracking releases only.",
        });
      } catch (err) {
        setActionMessage({ kind: "error", text: formatApiError(err) });
      } finally {
        setSavingChannel(false);
      }
    },
    [applyStatus, checkNow],
  );

  const checkedAt = formatCheckedAt(status?.latest_checked_at || "");
  const shortLatestCommit = (status?.latest_commit_sha || "").slice(0, 7);

  return (
    <Section heading="Updates">
      <Group>
        <Row
          label="Current version"
          hint={`Release v${__APP_VERSION__}`}
          control={
            <code style={{ fontSize: 12, color: "var(--text-primary)" }}>
              v{formatBuildVersion(__APP_VERSION__, __APP_BUILD_NUMBER__)} · {__APP_COMMIT__}
            </code>
          }
        />
        <Row
          label="Latest available"
          hint={
            !configured ? undefined : checkedAt ? `Last checked ${checkedAt}` : "Not checked yet"
          }
          control={
            loading ? (
              <Badge tone="muted">Loading…</Badge>
            ) : !configured ? (
              <Badge tone="muted">Not configured</Badge>
            ) : releaseAvailable ? (
              <Badge tone="warning" icon={<ArrowUpCircle size={12} strokeWidth={1.8} />}>
                v{status?.latest_version}
              </Badge>
            ) : commitAvailable ? (
              <Badge tone="warning" icon={<ArrowUpCircle size={12} strokeWidth={1.8} />}>
                new commit {shortLatestCommit}
              </Badge>
            ) : (
              <Badge tone="success" icon={<CheckCircle2 size={12} strokeWidth={1.8} />}>
                Up to date
              </Badge>
            )
          }
        />
        {updateAvailable && compareUrl && (
          <Row
            label="What's new"
            hint="See the commits this update would bring in, on GitHub."
            control={
              <a
                href={compareUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="glass-button"
                style={{
                  fontSize: 12,
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 6,
                  textDecoration: "none",
                }}
              >
                <ExternalLink size={13} strokeWidth={1.7} />
                View changes
              </a>
            }
          />
        )}
        {inProgress && (
          <Row
            label="Upgrade in progress"
            hint={status?.phase_detail || undefined}
            control={
              <Badge tone="warning" icon={<RotateCcw size={12} strokeWidth={1.8} />}>
                {status?.phase_progress || 0}%
              </Badge>
            }
          />
        )}
      </Group>

      <Group>
        <Row
          label="Allow updates from new commits"
          hint="Preview channel. When on, new commits on the main branch are surfaced too; when off, only tagged releases are checked."
          control={
            <Toggle
              checked={trackCommits}
              disabled={savingChannel || loading}
              onChange={handleToggleChannel}
              label="Allow updates from new commits"
            />
          }
        />
        <Row
          label="Check for updates"
          hint="Polls the git remote for a newer release tag (and new commits when the preview channel is on). Throttled to protect upstream."
          control={
            <button
              className="glass-button"
              onClick={handleCheck}
              disabled={checking}
              style={{ fontSize: 12, display: "inline-flex", alignItems: "center", gap: 6 }}
            >
              {checking ? (
                <Loader2 size={13} className="spin" />
              ) : (
                <RefreshCw size={13} strokeWidth={1.7} />
              )}
              {checking ? "Checking…" : "Check now"}
            </button>
          }
        />
        <Row
          label={updateAvailable ? "Update now" : "Manage upgrade"}
          hint={
            updateAvailable
              ? "Open the self-upgrade page to start the update, watch progress, or roll back."
              : "Open the self-upgrade page to start, monitor, or roll back an update."
          }
          control={
            <Link
              to="/upgrade"
              onClick={onClose}
              className={
                updateAvailable ? "glass-button glass-button--primary" : "glass-button"
              }
              style={{
                fontSize: 12,
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                textDecoration: "none",
              }}
            >
              {updateAvailable ? (
                <ArrowUpCircle size={13} strokeWidth={1.7} />
              ) : (
                <ExternalLink size={13} strokeWidth={1.7} />
              )}
              {updateAvailable ? "Update now" : "Open"}
            </Link>
          }
        />
      </Group>

      {!configured && !loading && (
        <StatusLine kind="info">
          No upgrade remote is available. Set <code>UPGRADE_GIT_REMOTE</code> on the Container App
          to point the control plane at a different upstream.
        </StatusLine>
      )}
      {failed && (
        <StatusLine kind="error">
          The last upgrade did not complete. Open the upgrade page to review the failure and roll
          back if needed.
        </StatusLine>
      )}
      {rolledBack && (
        <StatusLine kind="info">
          The last upgrade was rolled back. The control plane is running v
          {status?.running_version || "—"}.
        </StatusLine>
      )}
      {actionMessage && <StatusLine kind={actionMessage.kind}>{actionMessage.text}</StatusLine>}
      {error && <StatusLine kind="error">{error}</StatusLine>}
    </Section>
  );
}

function ResourcesSection({ config }: { config: ResourceConfig | null }) {
  const fields = [
    ["Subscription", config?.subscriptionId || "-"],
    ["Region", config?.region || "-"],
    ["Workload Resource Group", config?.workloadResourceGroup || "-"],
    ["Storage Account", config?.storageAccountName || "-"],
    ["ACR Resource Group", config?.acrResourceGroup || "-"],
    ["ACR Name", config?.acrName || "-"],
  ];
  return (
    <Section heading="Resources">
      <Group>
        <div style={{ padding: "12px 0" }}>
          {fields.map(([label, value]) => (
            <div key={label} style={{ display: "grid", gridTemplateColumns: "150px 1fr", gap: 12, padding: "6px 0", fontSize: 12 }}>
              <span style={{ color: "var(--text-faint)" }}>{label}</span>
              <span style={{ color: value === "-" ? "var(--text-faint)" : "var(--text-muted)", wordBreak: "break-all" }}>{value}</span>
            </div>
          ))}
        </div>
        <div style={{ borderTop: "1px solid var(--border-weak)", padding: "12px 0" }}>
          <button
            className="glass-button"
            onClick={() => {
              clearConfig();
              window.location.assign("/");
            }}
          >
            <RotateCcw size={12} strokeWidth={1.5} /> Re-run Setup Wizard
          </button>
        </div>
      </Group>
    </Section>
  );
}
