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

/** Visual state of a single lifecycle row. */
export type StageDisplay = "done" | "failed" | "canceled" | "pending";

/** Stages that only make sense on the success branch (running → succeeded →
 *  result delivered). When the job terminally fails these are either skipped
 *  (never reached) or — for ``completion_published`` — published as a FAILURE
 *  completion, so none of them represent a delivered result. */
const SUCCESS_PATH_STAGES = new Set([
  "running",
  "succeeded",
  "completion_published",
]);

/** True when the trace reached a terminal failure (``failed`` / ``dead_letter``). */
export function traceTerminallyFailed(reached: ReadonlySet<string>): boolean {
  return reached.has("failed") || reached.has("dead_letter");
}

/**
 * Resolve how a single stage row should render.
 *
 * - The terminal-failure stages (``failed`` / ``dead_letter``) render as
 *   ``failed`` when reached.
 * - On a terminally-failed job the success-branch stages render as
 *   ``canceled`` (grey) rather than a green success or an in-progress
 *   ``pending`` — including ``completion_published`` ("Result delivered"),
 *   which was published for a failure and so did NOT deliver a result.
 * - Otherwise a reached stage is ``done`` and an unreached one is ``pending``.
 */
export function stageDisplayState(
  stage: string,
  reached: ReadonlySet<string>,
  terminalFailed: boolean,
): StageDisplay {
  const isReached = reached.has(stage);
  if ((stage === "failed" || stage === "dead_letter") && isReached) {
    return "failed";
  }
  if (terminalFailed && stage === "completion_published") {
    return "canceled";
  }
  if (isReached) return "done";
  if (terminalFailed && SUCCESS_PATH_STAGES.has(stage)) return "canceled";
  return "pending";
}

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
