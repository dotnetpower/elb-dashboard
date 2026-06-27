/**
 * ServiceBusTelemetryPanel — pure presentation of the Service Bus telemetry
 * footer shown at the bottom of the Message Flow modal.
 *
 * Responsibility: render the queue/topic *raw* metrics (counts, size %, DLQ,
 * scheduled/transfer counters, queue status) over a {@link MessageFlowSnapshot}.
 * SRP: it does no fetching, no polling, no
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
  // Backlog sitting in the completion topic's subscriptions: `active` = completion
  // messages not yet consumed, `dlq` = completions that dead-lettered. These are
  // the real "is the completion path healthy?" signal — the topic name alone
  // tells an operator nothing about whether results are draining.
  const subActive = subs.reduce((acc, s) => acc + (s.active_message_count ?? 0), 0);
  const subDlq = subs.reduce(
    (acc, s) => acc + (s.dead_letter_message_count ?? 0),
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
              title="Request-queue dead-letter depth (separate from the completion topic's DLQ shown on the right). Persists until manually drained."
            >
              queue DLQ {queue?.dead_letter_message_count ?? 0}
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
          <span style={{ marginLeft: "auto" }} title="Completion topic and the backlog across its subscriptions: pending = completions not yet consumed, DLQ = completions that dead-lettered.">
            completions topic: {snapshot.completion_topic}
            {counts?.available ? (
              <>
                {" · "}
                <strong style={STRONG_STYLE}>{subActive}</strong> pending
                {subDlq > 0 ? (
                  <span style={{ color: "var(--warning)", marginLeft: 4 }}>
                    · DLQ {subDlq}
                  </span>
                ) : null}
              </>
            ) : null}
          </span>
        ) : null}
      </div>
    </div>
  );
}
