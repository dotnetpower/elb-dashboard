import type { BlastJobSummary, BlastResultFile } from "@/api/endpoints";
import {
  FAILURE_PHASES,
  PHASE_STEPS,
  inferFailedStepKey,
  isRecord,
} from "@/components/BlastStepTimeline";
import { statusColor } from "@/constants";

const DEBUG_FILES = new Set(["blast-status.txt", "jobs.txt", "pods.txt"]);
const RESULT_SUFFIXES = [".out", ".out.gz", ".xml", ".xml.gz", ".asn", ".asn.gz"];
const REPORT_SUFFIXES = [".json", ".jsonl"];
const REPORT_NAME_RE = /(manifest|report|summary|metrics|stats|metadata)/i;
const NON_ERROR_RUNNING_JOB_CODES = new Set(["blast_submit_lock_busy"]);

export type BlastResultFileKind = "result" | "support" | "diagnostic";

export function classifyBlastResultFile(file: BlastResultFile): BlastResultFileKind {
  const basename = file.name.split("/").pop() || "";
  const lowerName = file.name.toLowerCase();
  const lowerBase = basename.toLowerCase();
  if (
    DEBUG_FILES.has(basename) ||
    lowerBase.endsWith(".log") ||
    lowerBase.includes("stderr") ||
    lowerBase.includes("stdout") ||
    lowerBase === "status.txt"
  ) {
    return "diagnostic";
  }
  if (RESULT_SUFFIXES.some((suffix) => lowerBase.endsWith(suffix))) {
    return "result";
  }
  if (
    REPORT_SUFFIXES.some((suffix) => lowerBase.endsWith(suffix)) ||
    REPORT_NAME_RE.test(lowerName)
  ) {
    return "support";
  }
  return "support";
}

function sortResultFiles(files: BlastResultFile[]): BlastResultFile[] {
  return [...files].sort((a, b) => {
    const aName = a.name.toLowerCase();
    const bName = b.name.toLowerCase();
    const aMerged = aName.includes("merged_results") ? 0 : 1;
    const bMerged = bName.includes("merged_results") ? 0 : 1;
    if (aMerged !== bMerged) return aMerged - bMerged;
    const aTime = a.last_modified ? Date.parse(a.last_modified) : 0;
    const bTime = b.last_modified ? Date.parse(b.last_modified) : 0;
    if (aTime !== bTime) return bTime - aTime;
    return aName.localeCompare(bName);
  });
}

export function splitBlastResultFiles(allFiles: BlastResultFile[]) {
  const resultFiles = sortResultFiles(
    allFiles.filter((file) => classifyBlastResultFile(file) === "result"),
  );
  const supportFiles = sortResultFiles(
    allFiles.filter((file) => classifyBlastResultFile(file) === "support"),
  );
  const debugFiles = sortResultFiles(
    allFiles.filter((file) => classifyBlastResultFile(file) === "diagnostic"),
  );
  const visibleFiles =
    resultFiles.length > 0
      ? resultFiles
      : supportFiles.length > 0
        ? supportFiles
        : debugFiles;

  return {
    resultFiles,
    supportFiles,
    debugFiles,
    files: visibleFiles,
    hasOnlyDebugFiles:
      resultFiles.length === 0 && supportFiles.length === 0 && debugFiles.length > 0,
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
  const stagingStep = stepsObj?.staging_db as Record<string, unknown> | undefined;
  const hasOutputFiles = exportStep?.has_output_files as boolean | undefined;
  const submitOutput =
    ((submitStep?.output as string | undefined) ||
      (submitStep?.last_output as string | undefined) ||
      (stagingStep?.output as string | undefined) ||
      (stagingStep?.last_output as string | undefined) ||
      "");
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

export function shouldShowNonTerminalJobError(
  job: BlastJobSummary | undefined,
  phase: string,
): boolean {
  if (!job?.error || phase === "failed" || phase === "error") return false;
  const code = job.error_code || job.error;
  if (job.status === "running" && NON_ERROR_RUNNING_JOB_CODES.has(code)) {
    return false;
  }
  return true;
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
