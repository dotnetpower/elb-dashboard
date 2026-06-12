/**
 * ServiceBusInboundStrip — a read-only status line on Recent searches that
 * surfaces the optional Service Bus inbound queue when the integration is on.
 *
 * Renders NOTHING unless Service Bus is effective-enabled, so the default
 * (integration off) experience is unchanged. Shows the live request-queue and
 * dead-letter counts (best-effort; degrades to a plain label when the managed
 * identity lacks the Manage claim to read runtime counts) plus a "Manage"
 * affordance that opens the existing Settings → Service Bus section. It is
 * purely informational — all mutating actions live in that Settings section.
 */
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, ArrowRight, Radio } from "lucide-react";

import { settingsApi } from "@/api/settings";
import { useSettingsPanel } from "@/hooks/useSettingsPanel";

export function ServiceBusInboundStrip() {
  const { open } = useSettingsPanel();
  const query = useQuery({
    queryKey: ["service-bus-status"],
    queryFn: () => settingsApi.getServiceBus(),
    refetchInterval: 20_000,
    // Never surface a Service Bus fetch error as a page-level failure — the
    // strip just hides itself (the integration is optional).
    retry: false,
    staleTime: 10_000,
  });

  const data = query.data;
  // Hide entirely unless the integration is actually live.
  if (!data || !data.effective_enabled) return null;

  const counts = data.counts;
  const queueCount = counts?.queue?.active_message_count ?? null;
  const dlqCount = counts?.queue?.dead_letter_message_count ?? null;
  const countsAvailable = Boolean(counts?.available);

  return (
    <div
      className="glass-card"
      style={{
        display: "flex",
        alignItems: "center",
        gap: "var(--space-3)",
        padding: "8px 14px",
        fontSize: 12,
        color: "var(--text-muted)",
      }}
    >
      <Radio size={14} strokeWidth={1.5} style={{ color: "var(--accent)", flexShrink: 0 }} />
      <span style={{ color: "var(--text-primary)", fontWeight: 600 }}>Service Bus inbound</span>
      {countsAvailable ? (
        <>
          <span>
            queue <strong style={{ color: "var(--text-primary)" }}>{queueCount ?? "—"}</strong>
          </span>
          <span
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
              color: dlqCount && dlqCount > 0 ? "var(--warning)" : "var(--text-muted)",
            }}
          >
            DLQ <strong>{dlqCount ?? "—"}</strong>
            {dlqCount && dlqCount > 0 ? <AlertTriangle size={12} /> : null}
          </span>
        </>
      ) : (
        <span style={{ color: "var(--text-faint)" }}>
          counts unavailable{counts?.reason ? ` (${counts.reason})` : ""}
        </span>
      )}
      <button
        type="button"
        className="glass-button"
        onClick={() => open("service-bus")}
        style={{
          marginLeft: "auto",
          fontSize: 11,
          display: "inline-flex",
          alignItems: "center",
          gap: 4,
        }}
      >
        Manage <ArrowRight size={12} />
      </button>
    </div>
  );
}
