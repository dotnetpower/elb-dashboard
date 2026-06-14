/**
 * serviceBusTelemetryFormat — pure helpers behind the
 * {@link ServiceBusTelemetryPanel} renderer.
 *
 * Responsibility: format the Service Bus telemetry numbers (bytes, percent,
 * fill tone, DLQ growth summary) without touching React. Pulling these out
 * keeps the panel's JSX trivial and lets us unit-test the math — most
 * importantly the `size_pct` scale (backend ships percent on the 0..100
 * scale, e.g. `0.05` = 0.05 %, not a 0..1 fraction).
 */
import type { MessageFlowDlqDelta } from "@/api/messageFlow";

export function formatBytes(bytes: number | null): string {
  if (bytes == null || !Number.isFinite(bytes) || bytes <= 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const kb = bytes / 1024;
  if (kb < 1024) return `${kb.toFixed(1)} KB`;
  const mb = kb / 1024;
  if (mb < 1024) return `${mb.toFixed(1)} MB`;
  return `${(mb / 1024).toFixed(2)} GB`;
}

/** Format `size_pct`. Backend ships percent on the 0..100 scale, so we keep
 *  2 decimals — a healthy queue typically reads "0.05 %", not "0.0 %". */
export function formatPct(pct: number | null): string {
  if (pct == null || !Number.isFinite(pct)) return "—";
  return `${pct.toFixed(2)}%`;
}

/** Tone for the queue-fill percent (input already on 0..100 scale). Same
 *  50 % / 80 % thresholds the Storage card uses. */
export function fillTone(pct: number | null): string {
  if (pct == null) return "var(--text-muted)";
  if (pct >= 80) return "var(--danger)";
  if (pct >= 50) return "var(--warning)";
  return "var(--text-muted)";
}

export function statusTone(status: string | null | undefined): string {
  if (!status) return "var(--text-faint)";
  if (status === "Active") return "var(--success, var(--accent))";
  if (status === "Disabled") return "var(--danger)";
  return "var(--warning)";
}

export interface DlqDeltaSummary {
  text: string;
  tone: string;
}

/** Human label for the DLQ growth row. We never imply a higher resolution than
 *  the in-process rolling window actually gives us:
 *
 *  - `samples == 1` → baseline equals current; render "since first sample".
 *  - `delta == 0`  → quiet queue, render "no growth in last Ns".
 *  - `delta > 0`   → render "+N in last Ns" with the warning tone. */
export function dlqDeltaSummary(delta: MessageFlowDlqDelta): DlqDeltaSummary {
  const elapsed = Math.max(0, Math.round(delta.elapsed_seconds));
  if (delta.samples <= 1) {
    return {
      text: `${delta.current_dlq} since first sample`,
      tone: delta.current_dlq > 0 ? "var(--warning)" : "var(--text-muted)",
    };
  }
  if (delta.delta <= 0) {
    return {
      text: `no growth in last ${elapsed}s`,
      tone: "var(--text-muted)",
    };
  }
  return {
    text: `+${delta.delta} in last ${elapsed}s`,
    tone: "var(--warning)",
  };
}
