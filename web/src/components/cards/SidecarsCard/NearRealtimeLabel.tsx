export interface NearRealtimeLabelProps {
  source: "live" | "polling" | "connecting";
}

export function NearRealtimeLabel({ source }: NearRealtimeLabelProps) {
  const label =
    source === "live"
      ? "Near real-time · 5s"
      : source === "polling"
        ? "Polling · 30s"
        : "Connecting…";
  const dotColor = source === "live" ? "var(--accent)" : "var(--text-muted)";
  return (
    <span
      title={
        source === "live"
          ? "SSE stream pushing every 5s from /api/monitor/sidecars/events."
          : source === "polling"
            ? "SSE unavailable — falling back to /api/monitor/sidecars polling."
            : "Acquiring SSE ticket…"
      }
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        fontSize: 10,
        padding: "2px 8px",
        borderRadius: 999,
        background:
          source === "live"
            ? "rgba(122, 167, 255, 0.08)"
            : "rgba(154,163,184,0.08)",
        border:
          source === "live"
            ? "1px solid rgba(122, 167, 255, 0.22)"
            : "1px solid var(--border-weak)",
        color: "var(--text-muted)",
        whiteSpace: "nowrap",
      }}
    >
      <span
        aria-hidden
        style={{
          width: 6,
          height: 6,
          borderRadius: 999,
          background: dotColor,
          boxShadow:
            source === "live" ? "0 0 6px rgba(122,167,255,0.55)" : undefined,
        }}
      />
      {label}
    </span>
  );
}
