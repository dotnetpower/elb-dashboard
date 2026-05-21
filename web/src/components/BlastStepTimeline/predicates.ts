import { FAILURE_PHASES, PHASE_STEPS, PHASE_TO_STEP } from "./constants";

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function textValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (value == null) return "";
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export function stepHasEvidence(
  step: Record<string, unknown> | undefined,
): boolean {
  if (!step) return false;
  return Object.keys(step).length > 0;
}

export function stepHasFailure(
  step: Record<string, unknown> | undefined,
): boolean {
  if (!step) return false;
  if (step.success === false || step.auth_failed === true) return true;
  const text = [step.error, step.output, step.last_output].map(textValue).join("\n");
  return /(^|\n)\s*(ERROR:|FATAL|fatal|ErrorCode:|<Error>|Traceback|\u2717)/.test(text);
}

export function getFailureText(
  step: Record<string, unknown> | undefined,
  output: Record<string, unknown> | null,
  customStatus: Record<string, unknown> | null,
  job: Record<string, unknown>,
): string {
  // Order: authoritative orchestrator-emitted error first, then step-level
  // diagnostics, then live tail. Putting `step.last_output` last avoids
  // surfacing benign helper log lines (e.g. "Upload workfiles") as the
  // failure message when the real cause lives in `job.error` / `output.error`.
  const candidates = [
    job.error,
    output?.error,
    output?.message,
    customStatus?.error,
    customStatus?.message,
    step?.error,
    step?.output,
    step?.last_output,
  ];
  for (const candidate of candidates) {
    const text = textValue(candidate).trim();
    if (text) return text;
  }
  return "No detailed error was recorded by the orchestrator.";
}

export function inferFailedStepKey(
  phase: string,
  stepsData: Record<string, Record<string, unknown>>,
  output: Record<string, unknown> | null,
  customStatus: Record<string, unknown> | null,
): string | null {
  const mapped = PHASE_TO_STEP[phase] ?? phase;
  if (PHASE_STEPS.some((step) => step.key === mapped)) return mapped;

  const explicit = textValue(
    output?.failed_step ?? output?.step ?? customStatus?.failed_step,
  ).trim();
  const explicitMapped = PHASE_TO_STEP[explicit] ?? explicit;
  if (PHASE_STEPS.some((step) => step.key === explicitMapped)) return explicitMapped;

  for (const step of [...PHASE_STEPS].reverse()) {
    if (stepHasFailure(stepsData[step.key])) return step.key;
  }
  for (const step of [...PHASE_STEPS].reverse()) {
    if (stepHasEvidence(stepsData[step.key])) return step.key;
  }
  if (FAILURE_PHASES.has(phase)) return "submitting";
  return null;
}

export function firstErrorLine(text: string): string {
  return (
    text
      .split("\n")
      .find((line) =>
        /^\s*(ERROR:|FATAL|fatal|ErrorCode:|<Error>|Traceback|\u2717)/.test(line),
      ) ?? ""
  );
}
