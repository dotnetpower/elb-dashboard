import type { PropsWithChildren, ReactNode } from "react";

interface Props {
  title: string;
  subtitle?: ReactNode;
  status?: "idle" | "loading" | "ok" | "error";
  rightSlot?: ReactNode;
}

const STATUS_COLOR: Record<NonNullable<Props["status"]>, string> = {
  idle: "var(--text-faint)",
  loading: "var(--warning)",
  ok: "var(--success)",
  error: "var(--danger)",
};

export function MonitorCard({
  title,
  subtitle,
  status = "idle",
  rightSlot,
  children,
}: PropsWithChildren<Props>) {
  return (
    <section className="glass-card" style={{ minHeight: 200 }}>
      <header
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: "var(--space-4)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: "var(--space-3)" }}>
          <span
            aria-hidden
            style={{
              width: 8,
              height: 8,
              borderRadius: 999,
              background: STATUS_COLOR[status],
              boxShadow: `0 0 8px ${STATUS_COLOR[status]}`,
            }}
          />
          <div>
            <h3 style={{ margin: 0, fontSize: 14, letterSpacing: "0.04em", textTransform: "uppercase" }}>
              {title}
            </h3>
            {subtitle && (
              <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
                {subtitle}
              </div>
            )}
          </div>
        </div>
        {rightSlot}
      </header>
      <div>{children}</div>
    </section>
  );
}
