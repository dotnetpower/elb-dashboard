import { type PropsWithChildren, useState } from "react";
import { NavLink } from "react-router-dom";
import { useMsal } from "@azure/msal-react";
import { Activity, Terminal as TerminalIcon, LogOut, Search, List, Menu, X, Sun, Moon, HelpCircle } from "lucide-react";
import { Breadcrumb } from "@/components/Breadcrumb";
import { useKeyboardShortcuts, ShortcutOverlay } from "@/components/KeyboardShortcuts";
import { useTheme } from "@/hooks/useTheme";

import "./Layout.css";

export function Layout({ children }: PropsWithChildren) {
  const { instance, accounts } = useMsal();
  const account = accounts[0];
  const initials = (account?.name ?? account?.username ?? "U")
    .split(" ")
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();

  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const { showHelp, setShowHelp } = useKeyboardShortcuts();
  const { theme, toggle: toggleTheme } = useTheme();

  return (
    <div className="layout">
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
          <div className="layout__logo-icon" />
          <div>
            <div className="layout__logo-text">ElasticBLAST</div>
            <div className="layout__logo-sub">Control Plane</div>
          </div>
        </div>

        <nav className={`layout__nav${mobileNavOpen ? " layout__nav--open" : ""}`} aria-label="Main navigation">
          {/* #26 Visual grouping */}
          <span className="layout__nav-group-label">Monitor</span>
          <NavLink to="/" end className="layout__nav-item" onClick={() => setMobileNavOpen(false)}>
            <Activity size={14} strokeWidth={1.5} /> Dashboard
          </NavLink>
          <span className="layout__nav-sep" />
          <span className="layout__nav-group-label">BLAST</span>
          <NavLink to="/blast/submit" className="layout__nav-item" onClick={() => setMobileNavOpen(false)}>
            <Search size={14} strokeWidth={1.5} /> New Search
          </NavLink>
          <NavLink to="/blast/jobs" className="layout__nav-item" onClick={() => setMobileNavOpen(false)}>
            <List size={14} strokeWidth={1.5} /> Jobs
          </NavLink>
          <span className="layout__nav-sep" />
          <span className="layout__nav-group-label">Tools</span>
          <NavLink to="/terminal" className="layout__nav-item" onClick={() => setMobileNavOpen(false)}>
            <TerminalIcon size={14} strokeWidth={1.5} /> Terminal
          </NavLink>
        </nav>

        <div className="layout__spacer" />

        {/* #27 Live indicator with tooltip */}
        <div className="layout__live" title="Dashboard data refreshes automatically every 30 seconds">
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

        {/* #50 Theme toggle */}
        <button
          className="cfg-gear"
          onClick={toggleTheme}
          title={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
          style={{ marginLeft: 0 }}
        >
          {theme === "dark" ? <Sun size={14} /> : <Moon size={14} />}
        </button>

        <div className="layout__user-menu">
          <div
            className="layout__avatar"
            title={account?.username ?? "User"}
          >
            {initials}
          </div>
          <button
            className="glass-button"
            onClick={() => {
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
            style={{ padding: "4px 10px", fontSize: 11 }}
          >
            <LogOut size={12} strokeWidth={1.5} /> Sign out
          </button>
        </div>
      </header>

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
