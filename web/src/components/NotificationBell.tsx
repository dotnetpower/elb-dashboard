/**
 * NotificationBell — header bell with an unread badge and a dropdown feed.
 *
 * Renders recent terminal BLAST jobs (completed / failed / cancelled) from the
 * `useNotifications` hook. Opening the dropdown shows the feed; "Mark all read"
 * advances the server-side seen marker and clears the badge. Mirrors the
 * outside-click / Escape dismissal pattern used by `UserMenuDropdown`.
 */
import { useEffect, useRef, useState } from "react";
import { Bell, CheckCircle2, XCircle, Ban } from "lucide-react";

import { useNotifications } from "@/hooks/useNotifications";
import type { NotificationItem, NotificationStatus } from "@/api/notifications";

function statusVisual(status: NotificationStatus): { icon: React.ReactNode; color: string; label: string } {
  if (status === "completed") {
    return { icon: <CheckCircle2 size={15} strokeWidth={1.5} />, color: "var(--success, #4ade80)", label: "Completed" };
  }
  if (status === "failed") {
    return { icon: <XCircle size={15} strokeWidth={1.5} />, color: "var(--danger, #f87171)", label: "Failed" };
  }
  return { icon: <Ban size={15} strokeWidth={1.5} />, color: "var(--text-muted)", label: "Cancelled" };
}

function relativeTime(iso: string): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const diffMs = Date.now() - then;
  const sec = Math.max(0, Math.round(diffMs / 1000));
  if (sec < 60) return "just now";
  const min = Math.round(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.round(hr / 24);
  return `${day}d ago`;
}

function NotificationRow({ item }: { item: NotificationItem }) {
  const visual = statusVisual(item.status);
  const subtitle = [item.program, item.db].filter(Boolean).join(" · ");
  return (
    <div
      style={{
        display: "flex",
        gap: 10,
        padding: "10px 16px",
        borderBottom: "1px solid var(--border-weak)",
        background: item.unread ? "var(--bg-tertiary)" : "transparent",
        alignItems: "flex-start",
      }}
    >
      <span style={{ color: visual.color, marginTop: 1, flexShrink: 0 }}>{visual.icon}</span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
          <span style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {item.title || item.job_id}
          </span>
          <span style={{ fontSize: 11, color: "var(--text-faint)", flexShrink: 0 }}>{relativeTime(item.updated_at)}</span>
        </div>
        <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>
          {visual.label}
          {subtitle ? ` — ${subtitle}` : ""}
        </div>
        {item.status === "failed" && item.error_code && (
          <div style={{ fontSize: 11, color: "var(--danger, #f87171)", marginTop: 2, wordBreak: "break-word" }}>
            {item.error_code}
          </div>
        )}
      </div>
    </div>
  );
}

export function NotificationBell({ enabled }: { enabled: boolean }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const { items, unreadCount, isLoading, markSeen } = useNotifications(enabled);

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

  const badge = unreadCount > 99 ? "99+" : String(unreadCount);

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button
        className="cfg-gear"
        onClick={() => setOpen((o) => !o)}
        title={unreadCount > 0 ? `${unreadCount} new notification${unreadCount === 1 ? "" : "s"}` : "Notifications"}
        aria-label={unreadCount > 0 ? `Notifications, ${unreadCount} unread` : "Notifications"}
        style={{ marginLeft: 0, position: "relative" }}
      >
        <Bell size={14} strokeWidth={1.5} />
        {unreadCount > 0 && (
          <span
            role="img"
            aria-hidden="true"
            style={{
              position: "absolute",
              top: -4,
              right: -4,
              minWidth: 15,
              height: 15,
              padding: "0 3px",
              borderRadius: 8,
              background: "var(--danger, #f87171)",
              color: "#fff",
              fontSize: 9,
              fontWeight: 700,
              lineHeight: "15px",
              textAlign: "center",
            }}
          >
            {badge}
          </span>
        )}
      </button>

      {open && (
        <div
          style={{
            position: "absolute",
            top: "calc(100% + 8px)",
            right: 0,
            width: 360,
            maxHeight: 460,
            display: "flex",
            flexDirection: "column",
            background: "var(--bg-primary)",
            border: "1px solid var(--border-medium)",
            borderRadius: 12,
            boxShadow: "0 8px 32px rgba(0,0,0,0.4)",
            zIndex: 200,
            overflow: "hidden",
          }}
        >
          <div
            style={{
              padding: "10px 16px",
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              borderBottom: "1px solid var(--border-weak)",
              background: "var(--bg-tertiary)",
            }}
          >
            <span style={{ fontSize: 12, fontWeight: 600, color: "var(--text-primary)" }}>Notifications</span>
            {unreadCount > 0 && (
              <button
                onClick={() => markSeen()}
                style={{ background: "none", border: "none", color: "var(--accent)", cursor: "pointer", fontSize: 11, padding: 0 }}
              >
                Mark all read
              </button>
            )}
          </div>

          <div style={{ overflowY: "auto" }}>
            {isLoading && items.length === 0 ? (
              <div style={{ padding: "24px 16px", textAlign: "center", fontSize: 12, color: "var(--text-muted)" }}>
                Loading…
              </div>
            ) : items.length === 0 ? (
              <div style={{ padding: "28px 16px", textAlign: "center", fontSize: 12, color: "var(--text-muted)" }}>
                No job notifications yet.
              </div>
            ) : (
              items.map((item) => <NotificationRow key={item.job_id} item={item} />)
            )}
          </div>
        </div>
      )}
    </div>
  );
}
