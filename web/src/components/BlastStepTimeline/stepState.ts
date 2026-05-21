import { FAILURE_PHASES, PHASE_STEPS, PHASE_TO_STEP, type StepState } from "./constants";
import { stepHasEvidence, stepHasFailure } from "./predicates";

export function getTimelineStepState({
  phase,
  idx,
  key,
  stepsData,
  failedStepIdx,
}: {
  phase: string;
  idx: number;
  key: string;
  stepsData: Record<string, Record<string, unknown>>;
  failedStepIdx: number;
}): StepState {
  const step = stepsData[key];
  if (isStepSkipped(step)) return "skipped";
  if (isStepCompleted(step)) return "done";
  // A parent job in a failure phase must never render a spinner. Orphan
  // `status: "running"` step entries can survive when the worker crashes
  // mid-step and the top-level row is later reconciled to `failed` without
  // a payload merge. Resolve the failure path BEFORE isStepRunning so the
  // affected step shows as error/done/skipped instead of an infinite spin.
  if (FAILURE_PHASES.has(phase)) {
    if (failedStepIdx >= 0) {
      if (idx < failedStepIdx) return "done";
      if (idx === failedStepIdx) return "error";
      return "skipped";
    }
    if (stepHasFailure(step)) return "error";
    if (stepHasEvidence(step)) return "done";
    return "skipped";
  }
  if (isStepRunning(step)) return "active";
  if (phase === "completed") return "done";

  const currentPhaseIdx = resolveActiveStepIndex(phase, stepsData);
  if (currentPhaseIdx < 0) return "pending";
  if (idx < currentPhaseIdx) return "done";
  if (idx === currentPhaseIdx) return "active";
  return "pending";
}

export function resolveActiveStepIndex(
  phase: string,
  stepsData: Record<string, Record<string, unknown>>,
): number {
  const effectivePhaseKey = PHASE_TO_STEP[phase] ?? phase;
  let currentPhaseIdx = PHASE_STEPS.findIndex((step) => step.key === effectivePhaseKey);
  while (
    currentPhaseIdx >= 0 &&
    currentPhaseIdx < PHASE_STEPS.length - 1 &&
    isStepSkipped(stepsData[PHASE_STEPS[currentPhaseIdx].key])
  ) {
    currentPhaseIdx += 1;
  }
  return currentPhaseIdx;
}

export function isStepSkipped(step: Record<string, unknown> | undefined): boolean {
  if (!step) return false;
  return step.skipped === true || step.status === "skipped";
}

function isStepCompleted(step: Record<string, unknown> | undefined): boolean {
  if (!step) return false;
  return step.status === "completed" || step.status === "succeeded" || step.success === true;
}

function isStepRunning(step: Record<string, unknown> | undefined): boolean {
  if (!step) return false;
  return step.status === "running";
}