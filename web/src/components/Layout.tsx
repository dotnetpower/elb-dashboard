import { type PropsWithChildren, useState, useRef, useEffect } from "react";
import { NavLink } from "react-router-dom";
import { useMsal } from "@azure/msal-react";
import { Activity, Terminal as TerminalIcon, Search, List, Menu, X, HelpCircle, Code2, ArrowRightLeft, UserPlus, Database, AlertTriangle, LogIn, Dna, Settings as SettingsIcon } from "lucide-react";
import { Breadcrumb } from "@/components/Breadcrumb";
import { useKeyboardShortcuts, ShortcutOverlay } from "@/components/KeyboardShortcuts";
import { LatestJobChip } from "@/components/LatestJobChip";
import { NavMoreDropdown } from "@/components/NavMoreDropdown";
import { UpgradeBadge } from "@/components/UpgradeBadge";
import { loadSavedConfig } from "@/components/SetupWizard";
import { apiLoginRequest } from "@/auth/msal";
import { subscribeAuthSessionIssues, type AuthSessionIssue } from "@/auth/sessionEvents";
import { useClusterReadiness, useTerminalSidecarHealth } from "@/hooks/usePrerequisites";
import { useAutoRefreshInterval } from "@/hooks/useAutoRefresh";
import { useMediaQuery } from "@/hooks/useMediaQuery";
import { useSettingsPanel } from "@/hooks/useSettingsPanel";
import { usePreviewFeatureEnabled } from "@/hooks/usePreferences";
import { isFeatureEnabled } from "@/config/runtime";

import "./Layout.css";

function formatBuildVersion(releaseVersion: string, buildNumber: string): string {
  const parts = releaseVersion.split(".");
  if (parts.length !== 3 || !/^\d+$/.test(buildNumber)) {
    return releaseVersion;
  }
  return `${parts[0]}.${parts[1]}.${buildNumber}`;
}

// ---------------------------------------------------------------------------
// UserMenuDropdown — avatar click → popover with user info + sign out
// ---------------------------------------------------------------------------
function UserMenuDropdown({ account, initials, onSignOut }: {
  account: { name?: string; username?: string; tenantId?: string } | undefined;
  initials: string;
  onSignOut: () => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const savedConfig = loadSavedConfig();

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", handler);
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", handler); document.removeEventListener("keydown", esc); };
  }, [open]);

  const { instance } = useMsal();

  const handleSwitchDirectory = () => {
    setOpen(false);
    instance.loginRedirect({ scopes: [], prompt: "select_account" }).catch(() => {});
  };

  const handleSignInDifferent = () => {
    setOpen(false);
    instance.loginRedirect({ scopes: [], prompt: "login" }).catch(() => {});
  };

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button
        className="layout__avatar"
        onClick={() => setOpen(o => !o)}
        style={{ cursor: "pointer", border: "none" }}
        aria-label="User menu"
      >
        {initials}
      </button>

      {open && (
        <div style={{
          position: "absolute", top: "calc(100% + 8px)", right: 0,
          width: 380, background: "var(--bg-primary)",
          border: "1px solid var(--border-medium)", borderRadius: 12,
          boxShadow: "0 8px 32px rgba(0,0,0,0.4)", zIndex: 200,
          overflow: "hidden",
        }}>
          {/* Top bar — tenant name + sign out link */}
          <div style={{
            padding: "10px 18px", display: "flex", justifyContent: "space-between", alignItems: "center",
            borderBottom: "1px solid var(--border-weak)", background: "var(--bg-tertiary)",
          }}>
            <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
              {account?.tenantId ? `Directory: ${account.tenantId}` : "Microsoft Entra"}
            </span>
            <button
              onClick={() => { setOpen(false); onSignOut(); }}
              style={{
                background: "none", border: "none", color: "var(--accent)",
                cursor: "pointer", fontSize: 11, padding: 0,
              }}
            >
              Sign out
            </button>
          </div>

          {/* User info */}
          <div style={{ padding: "16px 18px", borderBottom: "1px solid var(--border-weak)" }}>
            <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
              <div style={{
                width: 48, height: 48, borderRadius: 50, fontSize: 18, fontWeight: 700,
                display: "grid", placeItems: "center", flexShrink: 0,
                background: "linear-gradient(135deg, var(--accent), var(--purple))",
                color: "#fff",
              }}>
                {initials}
              </div>
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 14, fontWeight: 600, lineHeight: 1.3 }}>
                  {account?.name || "User"}
                </div>
                <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 2, wordBreak: "break-all" }}>
                  {account?.username || ""}
                </div>
                {savedConfig?.workloadResourceGroup && (
                  <div style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 4 }}>
                    Workspace: {savedConfig.workloadResourceGroup}
                  </div>
                )}
              </div>
            </div>
          </div>

          {/* Actions — Azure Portal style */}
          <div style={{ padding: "6px 0" }}>
            <MenuAction icon={<ArrowRightLeft size={14} />} label="Switch directory" onClick={handleSwitchDirectory} />
            <MenuAction icon={<UserPlus size={14} />} label="Sign in with a different account" onClick={handleSignInDifferent} />
          </div>
        </div>
      )}
    </div>
  );
}

function MenuAction({ icon, label, onClick }: { icon: React.ReactNode; label: string; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      style={{
        width: "100%", display: "flex", alignItems: "center", gap: 10,
        padding: "10px 18px", background: "none", border: "none",
        color: "var(--text-muted)", cursor: "pointer", fontSize: 12,
        transition: "background 0.12s, color 0.12s", textAlign: "left",
      }}
      onMouseEnter={e => { e.currentTarget.style.background = "var(--bg-hover)"; e.currentTarget.style.color = "var(--text-primary)"; }}
      onMouseLeave={e => { e.currentTarget.style.background = "none"; e.currentTarget.style.color = "var(--text-muted)"; }}
    >
      <span style={{ color: "var(--text-faint)", display: "flex" }}>{icon}</span>
      {label}
    </button>
  );
}

function NavWarnDot() {
  return (
    <span
      aria-hidden
      style={{
        width: 6,
        height: 6,
        borderRadius: 999,
        background: "var(--warning)",
        marginLeft: 6,
        display: "inline-block",
        verticalAlign: "middle",
      }}
    />
  );
}

export function Layout({ children }: PropsWithChildren) {
  const { instance, accounts } = useMsal();
  const account = accounts[0];
  const [sessionIssue, setSessionIssue] = useState<AuthSessionIssue | null>(null);
  const initials = (account?.name ?? account?.username ?? "U")
    .split(" ")
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();

  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const { showHelp, setShowHelp } = useKeyboardShortcuts();
  const settingsPanel = useSettingsPanel();
  const autoRefreshMs = useAutoRefreshInterval();
  const autoRefreshLabel = autoRefreshMs >= 1000 ? `${Math.round(autoRefreshMs / 1000)}s` : `${autoRefreshMs}ms`;
  const cluster = useClusterReadiness();
  const customDbEnabled = usePreviewFeatureEnabled("customDb");
  const labToolsEnabled = usePreviewFeatureEnabled("labTools");
  const liveWallEnabled = usePreviewFeatureEnabled("liveWall");
  const terminalEnabled = isFeatureEnabled("terminal");
  // Responsive nav tiers. Tier A (>=1320 px) shows the full horizontal nav.
  // Tier B (720–1320 px) collapses the Tools group (Lab Tools / Terminal /
  // API) into a "More ▾" dropdown so Dashboard / New Search / Recent
  // searches stay first-class. Tier C (<720 px) falls back to the existing
  // hamburger drawer where every item is listed vertically.
  const isCompactNav = useMediaQuery("(max-width: 1320px)");
  const isMobileNav = useMediaQuery("(max-width: 720px)");
  const useToolsDropdown = isCompactNav && !isMobileNav;
  const terminalSidecar = useTerminalSidecarHealth(terminalEnabled);
  const clusterNeedsAttention = !cluster.isLoading && !cluster.isError && !cluster.hasRunningCluster;
  const clusterAttentionTitle = clusterNeedsAttention
    ? cluster.hasAnyCluster
      ? "AKS cluster is not running"
      : "No AKS cluster provisioned yet"
    : undefined;
  const terminalBlocked = !terminalSidecar.isHealthy;
  const showToolsGroup = labToolsEnabled || terminalEnabled;
  const buildVersion = formatBuildVersion(__APP_VERSION__, __APP_BUILD_NUMBER__);

  useEffect(() => subscribeAuthSessionIssues(setSessionIssue), []);

  const handleSignInAgain = () => {
    const activeAccount = instance.getActiveAccount() ?? account;
    setSessionIssue(null);
    instance.loginRedirect({
      ...apiLoginRequest,
      account: activeAccount,
      prompt: "login",
    }).catch(() => {
      setSessionIssue({
        reason: "token_refresh_failed",
        message: "Sign-in could not start. Refresh the browser and try again.",
      });
    });
  };

  return (
    <div className={`layout${mobileNavOpen ? " layout--mobile-nav-open" : ""}`}>
      <header className="layout__topbar">
        {/* #13 Hamburger for mobile */}
        <button
          className="layout__hamburger"
          onClick={() => setMobileNavOpen((p) => !p)}
          aria-label="Toggle navigation"
        >
          {mobileNavOpen ? <X size={20} /> : <Menu size={20} />}
        </button>

        <div className="layout__logo">
          <div className="layout__logo-icon" aria-hidden="true">
            <Dna size={18} strokeWidth={1.5} />
          </div>
          <div>
            <div className="layout__logo-text">ElasticBLAST</div>
            <div className="layout__logo-sub">
              <span>Control Plane</span>
              <span
                className="layout__logo-version"
                title={`Release: v${__APP_VERSION__}\nBuild: v${buildVersion}\nBuild number: ${__APP_BUILD_NUMBER__}\nCommit: ${__APP_COMMIT__}\nBuilt: ${__APP_BUILD_TIME__}`}
              >
                v{buildVersion} · {__APP_COMMIT__}
              </span>
              <UpgradeBadge />
            </div>
          </div>
        </div>

        <nav className={`layout__nav${mobileNavOpen ? " layout__nav--open" : ""}`} aria-label="Main navigation">
          {/* #26 Visual grouping */}
          <span className="layout__nav-group-label">Monitor</span>
          <NavLink
            to="/"
            end
            className="layout__nav-item"
            onClick={() => setMobileNavOpen(false)}
            title={clusterAttentionTitle}
          >
            <Activity size={14} strokeWidth={1.5} /> Dashboard
            {clusterNeedsAttention && <NavWarnDot />}
          </NavLink>
          {liveWallEnabled && (
            <NavLink to="/monitor/live-wall" className="layout__nav-item" onClick={() => setMobileNavOpen(false)}>
              <Activity size={14} strokeWidth={1.5} /> Live Wall
            </NavLink>
          )}
          <span className="layout__nav-sep" />
          <span className="layout__nav-group-label">BLAST</span>
          <NavLink
            to="/blast/submit"
            className="layout__nav-item"
            onClick={() => setMobileNavOpen(false)}
          >
            <Search size={14} strokeWidth={1.5} /> New Search
          </NavLink>
          <NavLink to="/blast/jobs" className="layout__nav-item" onClick={() => setMobileNavOpen(false)}>
            <List size={14} strokeWidth={1.5} /> Recent searches
          </NavLink>
          {customDbEnabled && (
            <NavLink to="/blast/databases/build" className="layout__nav-item" onClick={() => setMobileNavOpen(false)}>
              <Database size={14} strokeWidth={1.5} /> Custom DB
            </NavLink>
          )}
          {useToolsDropdown ? (
            <NavMoreDropdown
              label="Tools"
              title={[
                labToolsEnabled ? "Lab Tools" : null,
                terminalEnabled ? "Terminal" : null,
                "API",
              ]
                .filter(Boolean)
                .join(" · ")}
            >
              {labToolsEnabled && (
                <NavLink to="/tools" className="layout__nav-item" onClick={() => setMobileNavOpen(false)}>
                  <ArrowRightLeft size={14} strokeWidth={1.5} /> Lab Tools
                </NavLink>
              )}
              {terminalEnabled && (
                <NavLink
                  to="/terminal"
                  className="layout__nav-item"
                  onClick={() => setMobileNavOpen(false)}
                  title={terminalBlocked ? "Terminal sidecar is not available in this environment" : undefined}
                >
                  <TerminalIcon size={14} strokeWidth={1.5} /> Terminal
                  {terminalBlocked && <NavWarnDot />}
                </NavLink>
              )}
              <NavLink to="/docs" className="layout__nav-item" onClick={() => setMobileNavOpen(false)}>
                <Code2 size={14} strokeWidth={1.5} /> API
              </NavLink>
            </NavMoreDropdown>
          ) : (
            <>
              {showToolsGroup && <span className="layout__nav-sep" />}
              {showToolsGroup && <span className="layout__nav-group-label">Tools</span>}
              {labToolsEnabled && (
                <NavLink to="/tools" className="layout__nav-item" onClick={() => setMobileNavOpen(false)}>
                  <ArrowRightLeft size={14} strokeWidth={1.5} /> Lab Tools
                </NavLink>
              )}
              {terminalEnabled && (
                <NavLink
                  to="/terminal"
                  className="layout__nav-item"
                  onClick={() => setMobileNavOpen(false)}
                  title={terminalBlocked ? "Terminal sidecar is not available in this environment" : undefined}
                >
                  <TerminalIcon size={14} strokeWidth={1.5} /> Terminal
                  {terminalBlocked && <NavWarnDot />}
                </NavLink>
              )}
              <NavLink to="/docs" className="layout__nav-item" onClick={() => setMobileNavOpen(false)}>
                <Code2 size={14} strokeWidth={1.5} /> API
              </NavLink>
            </>
          )}
        </nav>

        <div className="layout__spacer" />

        {/* Researcher-first surface — latest BLAST job at a glance */}
        <LatestJobChip />

        {/* #27 Live indicator with tooltip */}
        <div
          className="layout__live"
          title={`Dashboard cards refresh every ${autoRefreshLabel} (configurable from the Dashboard header)`}
        >
          <div className="layout__live-dot" />
          Live
        </div>

        {/* #66 Keyboard shortcut hint */}
        <button
          className="cfg-gear"
          onClick={() => setShowHelp(true)}
          title="Keyboard shortcuts (?)"
          style={{ marginLeft: 0 }}
        >
          <HelpCircle size={14} />
        </button>

        {/* Settings */}
        <button
          className="cfg-gear"
          onClick={settingsPanel.open}
          title="Settings"
          style={{ marginLeft: 0 }}
        >
          <SettingsIcon size={14} />
        </button>

        <UserMenuDropdown
          account={account}
          initials={initials}
          onSignOut={() => {
            try {
              instance.logoutRedirect().catch(() => {
                sessionStorage.clear();
                localStorage.removeItem("elb-resource-config");
                window.location.href = "/";
              });
            } catch {
              sessionStorage.clear();
              localStorage.removeItem("elb-resource-config");
              window.location.href = "/";
            }
          }}
        />
      </header>

      {sessionIssue && (
        <div
          role="alert"
          style={{
            display: "flex",
            alignItems: "center",
            gap: 12,
            padding: "10px 24px",
            borderBottom: "1px solid var(--glass-border)",
            background: "rgba(240, 198, 116, 0.12)",
            color: "var(--text-primary)",
          }}
        >
          <AlertTriangle size={16} strokeWidth={1.5} style={{ color: "var(--warning)", flexShrink: 0 }} />
          <span style={{ flex: 1, fontSize: 13 }}>{sessionIssue.message}</span>
          <button
            onClick={handleSignInAgain}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              border: "1px solid var(--glass-border)",
              borderRadius: 8,
              padding: "6px 10px",
              background: "var(--glass-bg-strong)",
              color: "var(--text-primary)",
              cursor: "pointer",
              fontSize: 12,
              whiteSpace: "nowrap",
            }}
          >
            <LogIn size={13} strokeWidth={1.5} />
            Sign in again
          </button>
        </div>
      )}

      <main className="layout__main">
        {/* #11 Breadcrumb */}
        <Breadcrumb />
        {children}
      </main>

      {/* #16 Keyboard shortcuts overlay */}
      {showHelp && <ShortcutOverlay onClose={() => setShowHelp(false)} />}
    </div>
  );
}
