import type { PropsWithChildren, ReactNode } from "react";

interface Props {
  title: string;
  subtitle?: ReactNode;
  status?: "idle" | "loading" | "ok" | "error";
  rightSlot?: ReactNode;
}

const STATUS_TAG: Record<NonNullable<Props["status"]>, { cls: string; label: string } | null> = {
  idle: null,
  loading: { cls: "gt gt-o", label: "Loading" },
  ok: { cls: "gt gt-g", label: "OK" },
  error: { cls: "gt gt-r", label: "Error" },
};

export function MonitorCard({
  title,
  subtitle,
  status = "idle",
  rightSlot,
  children,
}: PropsWithChildren<Props>) {
  const tag = STATUS_TAG[status];

  return (
    <section className="panel">
      <div className="panel-hd">
        <div>
          <div className="title">{title}</div>
          {subtitle && <div className="sub">{subtitle}</div>}
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          {tag && <span className={tag.cls}>{tag.label}</span>}
          {rightSlot}
        </div>
      </div>
      <div className="panel-bd">{children}</div>
    </section>
  );
}
