import type { ReactNode } from "react";

export interface SectionHeaderProps {
  step: number;
  icon: ReactNode;
  title: string;
  subtitle?: string;
  rightSlot?: ReactNode;
}

export function SectionHeader({
  step,
  icon,
  title,
  subtitle,
  rightSlot,
}: SectionHeaderProps) {
  return (
    <div className="blast-section-hd" style={{ justifyContent: "space-between" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <span className="blast-step-badge">{step}</span>
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
