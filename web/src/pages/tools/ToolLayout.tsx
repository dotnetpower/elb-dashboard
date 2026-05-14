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
        {feature} needs a configured subscription, storage account, and Remote Terminal
        VM. Set them up on the Dashboard, then come back.
      </div>
      <Link to="/" className="btn btn--primary btn--sm" style={{ marginTop: 12 }}>
        Open Dashboard <ArrowRight size={12} />
      </Link>
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