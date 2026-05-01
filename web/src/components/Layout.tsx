import type { PropsWithChildren } from "react";
import { NavLink } from "react-router-dom";
import { useMsal } from "@azure/msal-react";
import { Activity, Terminal as TerminalIcon, LogOut, Search, List } from "lucide-react";

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

  return (
    <div className="layout">
      <header className="layout__topbar">
        <div className="layout__logo">
          <div className="layout__logo-icon" />
          <div>
            <div className="layout__logo-text">ElasticBLAST</div>
            <div className="layout__logo-sub">Control Plane</div>
          </div>
        </div>

        <nav className="layout__nav" aria-label="Main navigation">
          <NavLink to="/" end className="layout__nav-item">
            <Activity size={14} strokeWidth={1.5} /> Dashboard
          </NavLink>
          <NavLink to="/blast/submit" className="layout__nav-item">
            <Search size={14} strokeWidth={1.5} /> BLAST Search
          </NavLink>
          <NavLink to="/blast/jobs" className="layout__nav-item">
            <List size={14} strokeWidth={1.5} /> Jobs
          </NavLink>
          <NavLink to="/terminal" className="layout__nav-item">
            <TerminalIcon size={14} strokeWidth={1.5} /> Terminal
          </NavLink>
        </nav>

        <div className="layout__spacer" />

        <div className="layout__live">
          <div className="layout__live-dot" />
          Live
        </div>

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
                  // Fallback: clear MSAL cache and reload
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

      <main className="layout__main">{children}</main>
    </div>
  );
}
