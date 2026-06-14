/**
 * messageTraceModel — pure helpers for the Message lifecycle card.
 *
 * SRP: domain → view-model. No React, no I/O. Unit-testable in isolation.
 */
import type { BlastMessageTrace } from "@/api/blast.types";

export const STAGE_LABELS: Record<string, string> = {
  enqueued: "Enqueued",
  received: "Received",
  row_created: "Row created",
  routed: "Routed",
  submitted: "Submitted",
  running: "Running",
  succeeded: "Succeeded",
  failed: "Failed",
  completion_published: "Result delivered",
  dead_letter: "Dead-lettered",
};

export const CANONICAL_ORDER = [
  "enqueued",
  "received",
  "row_created",
  "routed",
  "submitted",
  "running",
  "succeeded",
  "failed",
  "completion_published",
  "dead_letter",
];

/** Human-friendly milliseconds: `—` for null, `ms` / `s` / `m s` otherwise. */
export function fmtTraceMs(ms: number | null): string {
  if (ms === null || !Number.isFinite(ms)) return "—";
  if (ms < 1000) return `${ms} ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)} s`;
  const m = Math.floor(s / 60);
  const rem = Math.round(s % 60);
  return `${m}m ${rem}s`;
}

/**
 * Canonical stages to render: every stage up to and including the last reached
 * one. Unreached intermediate stages stay visible (as pending) so the timeline
 * never has a gap; future stages beyond the last reached one are omitted.
 */
export function visibleTraceStages(trace: BlastMessageTrace): string[] {
  if (!trace.stages.length) return [];
  const reached = new Set(trace.stages.map((s) => s.stage));
  const lastIdx = Math.max(
    ...trace.stages.map((s) => CANONICAL_ORDER.indexOf(s.stage)),
  );
  return CANONICAL_ORDER.filter(
    (st, i) => i <= lastIdx && (reached.has(st) || i < lastIdx),
  );
}
