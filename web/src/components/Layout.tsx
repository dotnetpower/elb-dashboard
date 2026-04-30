import type { PropsWithChildren } from "react";
import { NavLink } from "react-router-dom";
import { useMsal } from "@azure/msal-react";
import { Activity, Terminal as TerminalIcon, LogOut, Search, List } from "lucide-react";

import "./Layout.css";

export function Layout({ children }: PropsWithChildren) {
  const { instance, accounts } = useMsal();
  const account = accounts[0];

  return (
    <div className="layout">
      <aside className="layout__sidebar glass-card glass-card--strong">
        <div className="layout__brand">
          <div className="layout__brand-mark" />
          <div>
            <div className="layout__brand-title">ElasticBLAST</div>
            <div className="muted layout__brand-sub">Control Plane</div>
          </div>
        </div>

        <nav className="layout__nav" aria-label="Main navigation">
          <NavLink to="/" end className="layout__nav-item">
            <Activity size={16} strokeWidth={1.5} /> Dashboard
          </NavLink>
          <NavLink to="/blast/submit" className="layout__nav-item">
            <Search size={16} strokeWidth={1.5} /> BLAST Search
          </NavLink>
          <NavLink to="/blast/jobs" className="layout__nav-item">
            <List size={16} strokeWidth={1.5} /> Jobs
          </NavLink>
          <NavLink to="/terminal" className="layout__nav-item">
            <TerminalIcon size={16} strokeWidth={1.5} /> Remote Terminal
          </NavLink>
        </nav>

        <div className="layout__user">
          <div className="muted" style={{ fontSize: 12 }}>
            Signed in
          </div>
          <div title={account?.username}>{account?.name ?? account?.username}</div>
          <button
            className="glass-button"
            onClick={() => instance.logoutRedirect()}
            style={{ marginTop: "var(--space-3)" }}
          >
            <LogOut size={14} strokeWidth={1.5} /> Sign out
          </button>
        </div>
      </aside>

      <main className="layout__main">{children}</main>
    </div>
  );
}
