import type { BlastJobSummary, BlastResultFile } from "@/api/endpoints";
import {
  FAILURE_PHASES,
  PHASE_STEPS,
  inferFailedStepKey,
  isRecord,
} from "@/components/BlastStepTimeline";
import { statusColor } from "@/constants";

const DEBUG_FILES = new Set(["blast-status.txt", "jobs.txt", "pods.txt"]);

export function splitBlastResultFiles(allFiles: BlastResultFile[]) {
  const resultFiles = allFiles.filter((file) => {
    const basename = file.name.split("/").pop() || "";
    return !DEBUG_FILES.has(basename) && !basename.endsWith(".log");
  });
  const debugFiles = allFiles.filter((file) => {
    const basename = file.name.split("/").pop() || "";
    return DEBUG_FILES.has(basename) || basename.endsWith(".log");
  });

  return {
    resultFiles,
    debugFiles,
    files: resultFiles.length > 0 ? resultFiles : debugFiles,
    hasOnlyDebugFiles: resultFiles.length === 0 && debugFiles.length > 0,
  };
}

export function resolveBlastJobPhase(job: BlastJobSummary | undefined) {
  const customStatus = isRecord(job?.custom_status) ? job.custom_status : null;
  const output = isRecord(job?.output) ? job.output : null;
  const outputPhase = output?.phase as string | undefined;
  const outputStatus = output?.status as string | undefined;
  const jobPhase =
    outputPhase || (customStatus?.phase as string) || job?.phase || job?.status;
  const isJobFailed = FAILURE_PHASES.has(jobPhase ?? "") || outputStatus === "failed";
  const phase = isJobFailed
    ? FAILURE_PHASES.has(jobPhase ?? "")
      ? (jobPhase as string)
      : "error"
    : job?.runtime_status === "Completed" && !isJobFailed
      ? "completed"
      : job?.runtime_status === "Failed"
        ? "error"
        : jobPhase || "unknown";

  return { customStatus, output, outputStatus, phase, isJobFailed };
}

export function resolveBlastResultState({
  job,
  phase,
  customStatus,
  output,
  outputStatus,
  isJobFailed,
}: {
  job: BlastJobSummary | undefined;
  phase: string;
  customStatus: Record<string, unknown> | null;
  output: Record<string, unknown> | null;
  outputStatus: string | undefined;
  isJobFailed: boolean;
}) {
  const isFailed = isJobFailed || FAILURE_PHASES.has(phase);
  const blastStatus = customStatus?.blast_status as string | undefined;
  const pollAttempt = customStatus?.poll_attempt as number | undefined;
  const runtimeStatus = job?.runtime_status as string | undefined;
  const stepsObj = (customStatus?.steps ?? output?.steps ?? {}) as Record<
    string,
    Record<string, unknown>
  >;
  const exportStep = stepsObj?.exporting_results as Record<string, unknown> | undefined;
  const submitStep = stepsObj?.submitting as Record<string, unknown> | undefined;
  const hasOutputFiles = exportStep?.has_output_files as boolean | undefined;
  const submitOutput = (submitStep?.output as string) ?? "";
  const submitHasFatalErrors = hasSubmitFatalErrors(submitOutput);
  const orchestratorSaysCompleted = outputStatus === "completed";
  const completedButFailed =
    phase === "completed" &&
    !orchestratorSaysCompleted &&
    (hasOutputFiles === false || submitHasFatalErrors);
  const effectivePhase = completedButFailed ? "submit_failed" : phase;
  const effectiveIsFailed = isFailed || completedButFailed;
  const effectiveColor = statusColor(
    effectivePhase === "submit_failed" ? "failed" : effectivePhase,
  );
  const isRunning =
    Boolean(job) &&
    !effectiveIsFailed &&
    phase !== "completed" &&
    phase !== "deleted" &&
    phase !== "cancelled";
  const failedStepKey = effectiveIsFailed
    ? inferFailedStepKey(effectivePhase, stepsObj, output, customStatus)
    : null;
  const failedStepLabel = failedStepKey
    ? (PHASE_STEPS.find((step) => step.key === failedStepKey)?.label ?? "Execution")
    : "Execution";

  return {
    blastStatus,
    pollAttempt,
    runtimeStatus,
    stepsObj,
    submitOutput,
    completedButFailed,
    effectivePhase,
    effectiveIsFailed,
    effectiveColor,
    isRunning,
    failedStepKey,
    failedStepLabel,
  };
}

function hasSubmitFatalErrors(submitOutput: string): boolean {
  if (/ErrorCode:|<Error>/.test(submitOutput)) return true;
  const nonFatal = ["Unrecognized configuration parameter"];
  for (const line of submitOutput.split("\n")) {
    if (line.startsWith("ERROR:")) {
      const isFatal = !nonFatal.some((value) => line.includes(value));
      if (isFatal) return true;
    }
  }
  return false;
}