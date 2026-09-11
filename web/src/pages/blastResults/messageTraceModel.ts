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
  transition_published: "Running delivered",
  succeeded: "Succeeded",
  failed: "Failed",
  completion_published: "Terminal status delivered",
  dead_letter: "Dead-lettered",
};

export const CANONICAL_ORDER = [
  "enqueued",
  "received",
  "row_created",
  "routed",
  "submitted",
  "running",
  "transition_published",
  "succeeded",
  "failed",
  "completion_published",
  "dead_letter",
];

/** Visual state of a single lifecycle row. */
export type StageDisplay = "done" | "failed" | "canceled" | "pending";

/** Stages that only make sense on the success branch. */
const SUCCESS_PATH_STAGES = new Set(["running", "transition_published", "succeeded"]);
const TERMINAL_BRANCH_STAGES = new Set(["succeeded", "failed", "dead_letter"]);

function terminalBranch(
  trace: BlastMessageTrace,
  reached: ReadonlySet<string>,
): string | null {
  const declared = trace.terminal_stage;
  if (declared && TERMINAL_BRANCH_STAGES.has(declared) && reached.has(declared)) {
    return declared;
  }
  const candidates = trace.stages
    .filter(({ stage }) => TERMINAL_BRANCH_STAGES.has(stage))
    .map(({ stage, ts }) => ({ stage, time: Date.parse(ts) }))
    .filter(({ time }) => Number.isFinite(time))
    .sort(
      (left, right) =>
        left.time - right.time ||
        CANONICAL_ORDER.indexOf(left.stage) - CANONICAL_ORDER.indexOf(right.stage),
    );
  if (candidates[0]) return candidates[0].stage;
  return (
    CANONICAL_ORDER.find(
      (stage) => TERMINAL_BRANCH_STAGES.has(stage) && reached.has(stage),
    ) ?? null
  );
}

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
 *   ``pending``. A reached ``completion_published`` remains done because it
 *   means the terminal failure status was delivered to subscribers.
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
  const resolvedTerminalBranch = terminalBranch(trace, reached);
  const lastIdx = Math.max(...trace.stages.map((s) => CANONICAL_ORDER.indexOf(s.stage)));
  return CANONICAL_ORDER.filter(
    (stage, index) =>
      index <= lastIdx &&
      (reached.has(stage) || index < lastIdx) &&
      (resolvedTerminalBranch === null ||
        !TERMINAL_BRANCH_STAGES.has(stage) ||
        stage === resolvedTerminalBranch),
  );
}
