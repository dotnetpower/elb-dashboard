import type { ReactNode } from "react";
import { AlertTriangle, ArrowRight } from "lucide-react";
import { Link } from "react-router-dom";

export function SectionHeader({
  icon,
  title,
  subtitle,
  rightSlot,
}: {
  icon: ReactNode;
  title: string;
  subtitle?: string;
  rightSlot?: ReactNode;
}) {
  return (
    <div className="blast-section-hd" style={{ justifyContent: "space-between" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <span className="blast-section-icon">{icon}</span>
        <div>
          <div className="blast-section-title">{title}</div>
          {subtitle && <div className="blast-section-sub">{subtitle}</div>}
        </div>
      </div>
      {rightSlot && <div>{rightSlot}</div>}
    </div>
  );
}

export function SetupRequired({ feature }: { feature: string }) {
  return (
    <div
      className="empty-state"
      style={{ borderRadius: 12, border: "1px dashed var(--border-medium)" }}
    >
      <div className="empty-state__icon">
        <AlertTriangle size={20} strokeWidth={1.5} />
      </div>
      <div className="empty-state__title">Workspace not configured</div>
      <div className="empty-state__desc">
        {feature} needs a configured subscription and storage account. Set them up on
        the Dashboard, then come back.
      </div>
      <Link to="/" className="btn btn--primary btn--sm" style={{ marginTop: 12 }}>
        Open Dashboard <ArrowRight size={12} />
      </Link>
    </div>
  );
}

export function SidecarRequired({ feature }: { feature: string }) {
  return (
    <div
      className="empty-state"
      style={{ borderRadius: 12, border: "1px dashed var(--border-medium)" }}
    >
      <div className="empty-state__icon">
        <AlertTriangle size={20} strokeWidth={1.5} />
      </div>
      <div className="empty-state__title">Terminal sidecar unavailable</div>
      <div className="empty-state__desc">
        {feature} runs inside the in-process <code>terminal</code> sidecar. The sidecar
        is not reachable in this environment — deploy the Container App (or run the
        local docker-compose stack) and try again.
      </div>
    </div>
  );
}

export function NotImplementedNotice({ feature }: { feature: string }) {
  return (
    <div
      className="empty-state"
      style={{ borderRadius: 12, border: "1px dashed var(--border-medium)" }}
    >
      <div className="empty-state__icon">
        <AlertTriangle size={20} strokeWidth={1.5} />
      </div>
      <div className="empty-state__title">Backend not implemented yet</div>
      <div className="empty-state__desc">
        {feature} does not have a backend route in this build. The UI is preserved for
        when the corresponding Celery task lands.
      </div>
    </div>
  );
}

export function NotImplementedBanner({ feature }: { feature: string }) {
  return (
    <div
      role="status"
      style={{
        marginBottom: 12,
        padding: "8px 12px",
        borderRadius: 8,
        border: "1px solid var(--border-medium)",
        background: "rgba(255,193,7,0.06)",
        display: "flex",
        alignItems: "center",
        gap: 8,
        fontSize: 12,
        color: "var(--text-muted)",
      }}
    >
      <AlertTriangle size={14} strokeWidth={1.5} style={{ color: "var(--warning)" }} />
      <span>
        <strong style={{ color: "var(--warning)" }}>Preview only.</strong> {feature}{" "}
        backend is not implemented in this build — submitting will return a
        <code style={{ margin: "0 4px" }}>503 lab_tool_backend_pending</code> error.
      </span>
    </div>
  );
}

export function StatBox({
  label,
  value,
  accent,
}: {
  label: string;
  value: string | number;
  accent?: boolean;
}) {
  return (
    <div className="metric-block">
      <div className="mv" style={accent ? { color: "var(--accent)" } : undefined}>
        {value}
      </div>
      <div className="mu">{label}</div>
    </div>
  );
}