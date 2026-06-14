/**
 * ServiceBusTelemetryPanel — pure presentation of the Service Bus telemetry
 * footer shown at the bottom of the Message Flow modal.
 *
 * Responsibility: render the queue/topic *raw* metrics (counts, size %, DLQ,
 * scheduled/transfer counters, queue status, rolling DLQ-growth delta) over
 * a {@link MessageFlowSnapshot}. SRP: it does no fetching, no polling, no
 * caching — the parent modal owns the data and just hands it down. Replaces
 * the inline footer that used to live inside MessageFlowModal.tsx.
 *
 * The panel is intentionally tolerant: every telemetry field is optional and
 * a missing/None value renders as an em dash so an older backend snapshot or
 * a partially-degraded admin call (e.g. `get_queue` failed but counters came
 * through) never breaks the modal — it just shows less.
 */
import type { CSSProperties } from "react";

import type { MessageFlowSnapshot } from "@/api/messageFlow";

import {
  dlqDeltaSummary,
  fillTone,
  formatBytes,
  formatPct,
  statusTone,
} from "./serviceBusTelemetryFormat";

interface ServiceBusTelemetryPanelProps {
  snapshot: MessageFlowSnapshot;
}

const ROW_STYLE: CSSProperties = {
  marginTop: 18,
  paddingTop: 12,
  borderTop: "1px solid var(--glass-border)",
  fontSize: 11,
  color: "var(--text-muted)",
  display: "flex",
  flexDirection: "column",
  gap: 8,
};

const ITEMS_STYLE: CSSProperties = {
  display: "flex",
  gap: 16,
  flexWrap: "wrap",
  alignItems: "center",
};

const STRONG_STYLE: CSSProperties = { color: "var(--text-primary)" };

export function ServiceBusTelemetryPanel({ snapshot }: ServiceBusTelemetryPanelProps) {
  const counts = snapshot.sb_counts;
  const queue = counts?.queue ?? null;
  const telemetry = queue?.telemetry ?? null;
  const dlqDelta = snapshot.dlq_delta ?? null;

  // Sum of forwarded / DLQ-of-transfer messages across the completion topic's
  // subscriptions — a non-zero value means the auto-forward path is in use,
  // which is itself a useful signal to surface even when raw counts are small.
  const subs = counts?.subscriptions ?? [];
  const transferOut = subs.reduce(
    (acc, s) => acc + (s.transfer_message_count ?? 0),
    0,
  );
  const transferDlq = subs.reduce(
    (acc, s) => acc + (s.transfer_dead_letter_message_count ?? 0),
    0,
  );

  return (
    <div style={ROW_STYLE} data-testid="sb-telemetry-panel">
      <div style={ITEMS_STYLE}>
        <span>
          Service Bus:{" "}
          <strong style={STRONG_STYLE}>
            {snapshot.enabled ? "enabled" : "disabled"}
          </strong>
        </span>
        {counts?.available ? (
          <>
            <span>
              queue{" "}
              <strong style={STRONG_STYLE}>
                {queue?.active_message_count ?? "—"}
              </strong>{" "}
              active
            </span>
            <span>{queue?.scheduled_message_count ?? 0} scheduled</span>
            <span
              style={{
                color:
                  (queue?.dead_letter_message_count ?? 0) > 0
                    ? "var(--warning)"
                    : "var(--text-muted)",
              }}
              title="Dead-letter queue depth. Persists until manually drained."
            >
              DLQ {queue?.dead_letter_message_count ?? 0}
            </span>
            {telemetry ? (
              <>
                <span
                  style={{ color: fillTone(telemetry.size_pct) }}
                  title={
                    telemetry.max_size_in_mb != null
                      ? `${formatBytes(telemetry.size_in_bytes)} of ${telemetry.max_size_in_mb} MB`
                      : undefined
                  }
                >
                  size {formatBytes(telemetry.size_in_bytes)}
                  {telemetry.size_pct != null
                    ? ` (${formatPct(telemetry.size_pct)})`
                    : ""}
                </span>
                {telemetry.status ? (
                  <span
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 5,
                      color: statusTone(telemetry.status),
                    }}
                    title="Queue entity status from Service Bus admin metadata."
                  >
                    <span
                      style={{
                        width: 7,
                        height: 7,
                        borderRadius: "50%",
                        background: statusTone(telemetry.status),
                      }}
                    />
                    {telemetry.status.toLowerCase()}
                  </span>
                ) : null}
              </>
            ) : null}
            {transferOut > 0 || transferDlq > 0 ? (
              <span
                title="Auto-forward path: messages transferred out of this entity, and forwarded messages that landed in their target DLQ."
              >
                transfer {transferOut}
                {transferDlq > 0 ? (
                  <span
                    style={{ color: "var(--warning)", marginLeft: 4 }}
                  >
                    / DLQ {transferDlq}
                  </span>
                ) : null}
              </span>
            ) : null}
          </>
        ) : (
          <span style={{ color: "var(--text-faint)" }}>
            counts unavailable{counts?.reason ? ` (${counts.reason})` : ""}
          </span>
        )}
        {snapshot.completion_topic ? (
          <span style={{ marginLeft: "auto" }}>
            completions topic: {snapshot.completion_topic}
          </span>
        ) : null}
      </div>

      {dlqDelta ? (
        <div
          style={{
            display: "flex",
            gap: 10,
            alignItems: "center",
            fontSize: 11,
          }}
          data-testid="sb-dlq-delta"
        >
          <span style={{ color: "var(--text-faint)" }}>DLQ growth</span>
          {(() => {
            const summary = dlqDeltaSummary(dlqDelta);
            return (
              <span style={{ color: summary.tone, fontWeight: 600 }}>
                {summary.text}
              </span>
            );
          })()}
          <span style={{ color: "var(--text-faint)" }}>
            ({dlqDelta.samples} sample{dlqDelta.samples === 1 ? "" : "s"} in last{" "}
            {Math.round(dlqDelta.window_seconds / 60)}m window)
          </span>
        </div>
      ) : null}
    </div>
  );
}
