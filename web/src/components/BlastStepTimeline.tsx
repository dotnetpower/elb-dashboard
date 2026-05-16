import { useState, useRef, useEffect } from "react";
import {
  CheckCircle2,
  XCircle,
  Copy,
  Check,
  ChevronRight,
  ChevronDown,
  Loader2,
  Server,
  HardDrive,
  Upload,
  Settings,
  Dna,
  Send,
  Package,
  Trophy,
} from "lucide-react";

import { FilePreview } from "@/components/BlastFilePreview";

// Shimmer animation for active steps
const shimmerStyle = `
@keyframes step-shimmer {
  0% { transform: translateX(-100%); }
  100% { transform: translateX(300%); }
}
`;

// Phase steps matching orchestrator order exactly
export const PHASE_STEPS = [
  {
    key: "checking_vm",
    label: "Prepare VM",
    desc: "Start remote terminal",
    icon: Server,
  },
  {
    key: "enabling_storage",
    label: "Open Storage",
    desc: "Enable public access",
    icon: HardDrive,
  },
  {
    key: "uploading",
    label: "Upload Query",
    desc: "Send sequence to blob",
    icon: Upload,
  },
  { key: "configuring", label: "Configure", desc: "Generate INI config", icon: Settings },
  { key: "warming_up", label: "Warmup", desc: "Prepare DB shards on SSD", icon: Dna },
  { key: "submitting", label: "Submit Job", desc: "Send to AKS cluster", icon: Send },
  { key: "running", label: "BLAST Run", desc: "Sequence alignment", icon: Dna },
  {
    key: "exporting_results",
    label: "Export",
    desc: "Copy results to blob",
    icon: Package,
  },
  { key: "completed", label: "Complete", desc: "All done!", icon: Trophy },
];

export const FAILURE_PHASES = new Set([
  "failed",
  "error",
  "submit_failed",
  "split_submit_invalid",
  "split_results_merge_invalid",
  "warmup_failed",
]);

type StepState = "done" | "active" | "pending" | "error" | "skipped";

const PHASE_TO_STEP: Record<string, string> = {
  submit_failed: "submitting",
  reading_split_query: "uploading",
  splitting_queries: "configuring",
  split_children_submitted: "submitting",
  split_children_aggregating: "running",
  split_children_merge_ready: "exporting_results",
  split_results_waiting_for_artifacts: "exporting_results",
  split_results_merging: "exporting_results",
  split_submit_invalid: "submitting",
  split_results_merge_invalid: "exporting_results",
  warmup_failed: "warming_up",
};

export const PHASE_MESSAGES: Record<string, string> = {
  checking_vm: "Verifying Terminal sidecar is reachable...",
  enabling_storage: "Enabling storage public access for data transfer...",
  uploading: "Uploading query sequence to Azure Blob Storage...",
  configuring: "Generating ElasticBLAST configuration...",
  warming_up: "Preparing cluster with DB shards on local SSD (warmup)...",
  warmup_failed: "Cluster warmup failed.",
  submitting: "Submitting job to AKS cluster...",
  reading_split_query: "Reading the original query from Storage...",
  splitting_queries: "Splitting queries by effective search space...",
  split_children_submitted: "Submitted split child jobs to AKS...",
  split_children_aggregating: "Waiting for split child jobs to finish...",
  split_children_merge_ready: "Split child jobs are ready for result assembly...",
  split_results_waiting_for_artifacts: "Waiting for split child result artifacts...",
  split_results_merging: "Assembling split child results...",
  split_submit_invalid: "Split submit request is invalid.",
  split_results_merge_invalid: "Split result assembly failed validation.",
  running: "BLAST search is running on the cluster...",
  exporting_results: "Verifying result files and exporting logs from cluster...",
  completed: "Job completed successfully!",
  failed: "Job failed.",
  error: "An error occurred.",
};

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function textValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (value == null) return "";
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function stepHasEvidence(step: Record<string, unknown> | undefined): boolean {
  if (!step) return false;
  return Object.keys(step).length > 0;
}

function stepHasFailure(step: Record<string, unknown> | undefined): boolean {
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
  const candidates = [
    step?.error,
    step?.output,
    step?.last_output,
    output?.error,
    output?.message,
    customStatus?.error,
    customStatus?.message,
    job.error,
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

// Premium log block with line numbers, syntax coloring, scrolling, and copy
function StepLogBlock({
  log,
  state,
  stepKey,
}: {
  log: string;
  state: StepState;
  stepKey: string;
}) {
  const [copied, setCopied] = useState(false);
  const [isExpanded, setIsExpanded] = useState(false);

  // Split into summary (first line) and detail (rest with line numbers)
  const delimIdx = log.indexOf("---");
  const hasSections = delimIdx > 0;
  const allLines = log.split("\n");
  // Summary = text before the first "---" section, or first line if multi-line without "---"
  const summary = hasSections
    ? log.slice(0, delimIdx).trim()
    : allLines.length <= 2
      ? log
      : allLines[0];
  // Detail = everything from "---" onward, or lines 2+ if no "---" but multi-line
  const detail = hasSections
    ? log.slice(delimIdx).trim()
    : allLines.length > 2
      ? allLines.slice(1).join("\n")
      : null;
  const detailLines = detail?.split("\n") ?? [];
  const isLong = detailLines.length > 40;

  const copyLog = () => {
    navigator.clipboard.writeText(log).catch(() => {});
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="step-log-block" data-state={state}>
      {/* Summary line */}
      <div className="step-log-summary">
        <span>{summary}</span>
        <button className="step-log-copy" onClick={copyLog} title="Copy full log">
          {copied ? (
            <Check size={11} strokeWidth={2} />
          ) : (
            <Copy size={11} strokeWidth={1.5} />
          )}
          <span>{copied ? "Copied" : "Copy"}</span>
        </button>
      </div>

      {/* Detail/console output */}
      {detail && (
        <div
          className={`step-log-detail${isLong && !isExpanded ? " step-log-detail--collapsed" : ""}`}
        >
          <div className="step-log-lines">
            {(isLong && !isExpanded ? detailLines.slice(0, 40) : detailLines).map(
              (line, i) => {
                let lineClass = "step-log-text";
                if (line.startsWith("WARNING") || line.startsWith("⚠"))
                  lineClass += " step-log-text--warn";
                else if (
                  line.startsWith("ERROR") ||
                  line.startsWith("✗") ||
                  /ErrorCode:|<Error>|ContainerNotFound|FATAL/.test(line)
                )
                  lineClass += " step-log-text--error";
                else if (
                  line.startsWith("✓") ||
                  line.includes("=ok") ||
                  line.includes("EXIT_CODE=0")
                )
                  lineClass += " step-log-text--ok";
                else if (line.startsWith("---")) lineClass += " step-log-text--header";
                else if (line.startsWith("INFO:")) lineClass += " step-log-text--info";
                return (
                  <div key={`${stepKey}-${i}`} className="step-log-line">
                    <span className="step-log-ln">{i + 1}</span>
                    <span className={lineClass}>{line || "\u00A0"}</span>
                  </div>
                );
              },
            )}
          </div>
          {isLong && !isExpanded && (
            <button className="step-log-expand" onClick={() => setIsExpanded(true)}>
              Show all {detailLines.length} lines
            </button>
          )}
          {isLong && isExpanded && (
            <button className="step-log-expand" onClick={() => setIsExpanded(false)}>
              Collapse
            </button>
          )}
        </div>
      )}
    </div>
  );
}

export function StepLogSection({
  phase,
  job,
  subscriptionId,
  storageAccount,
}: {
  phase: string;
  job: Record<string, unknown>;
  subscriptionId: string;
  storageAccount: string;
}) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [, setTick] = useState(0);
  const phaseTimestamps = useRef<Record<string, number>>({});
  const phaseDurations = useRef<Record<string, number>>({});
  const customStatus =
    typeof job?.custom_status === "object" && job?.custom_status !== null
      ? (job.custom_status as Record<string, unknown>)
      : null;
  const output = isRecord(job?.output) ? job.output : null;
  const stepsData = (customStatus?.steps ??
    (output as Record<string, unknown>)?.steps ??
    {}) as Record<string, Record<string, unknown>>;
  const jobId = job?.job_id as string;

  // Track phase transitions to calculate per-step durations
  useEffect(() => {
    if (!phase) return;
    const now = Date.now();
    const ts = phaseTimestamps.current;

    // Find the step key that matches the current phase
    const stepIdx = PHASE_STEPS.findIndex((s) => s.key === phase);
    if (stepIdx >= 0 && !ts[phase]) {
      ts[phase] = now;
      // Mark previous step as completed with duration
      if (stepIdx > 0) {
        const prevKey = PHASE_STEPS[stepIdx - 1].key;
        if (ts[prevKey] && !phaseDurations.current[prevKey]) {
          phaseDurations.current[prevKey] = now - ts[prevKey];
        }
      }
    }
    // If completed/failed, finalize all durations
    if (phase === "completed" || phase === "failed") {
      for (let i = 0; i < PHASE_STEPS.length; i++) {
        const key = PHASE_STEPS[i].key;
        if (ts[key] && !phaseDurations.current[key]) {
          const nextKey = PHASE_STEPS[i + 1]?.key;
          const endTime = nextKey && ts[nextKey] ? ts[nextKey] : now;
          phaseDurations.current[key] = endTime - ts[key];
        }
      }
    }
  }, [phase]);

  // Tick every second to update active step timer
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  const formatDuration = (ms: number): string => {
    const s = Math.round(ms / 1000);
    if (s >= 3600)
      return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m ${s % 60}s`;
    return s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`;
  };

  const getStepDuration = (key: string, state: StepState): string | null => {
    if (state === "pending" || state === "skipped") return null;

    // 1. Prefer server-side timestamps (available for completed jobs)
    const sd = stepsData[key] as Record<string, unknown> | undefined;
    if (sd?.started_at && sd?.completed_at) {
      const ms =
        new Date(sd.completed_at as string).getTime() -
        new Date(sd.started_at as string).getTime();
      if (ms >= 0) return formatDuration(ms);
    }
    // Server-side started_at but no completed_at → live elapsed from server start
    if (sd?.started_at && state === "active") {
      const ms = Date.now() - new Date(sd.started_at as string).getTime();
      return formatDuration(Math.max(0, ms));
    }

    // 2. Fall back to client-side tracking (live sessions)
    const dur = phaseDurations.current[key];
    if (dur) return formatDuration(dur);

    // Active step — show live elapsed from client timestamp
    if (state === "active") {
      const start = phaseTimestamps.current[key];
      if (start) return formatDuration(Date.now() - start);
    }
    return null;
  };

  const toggle = (key: string) => setExpanded((prev) => ({ ...prev, [key]: !prev[key] }));

  const effectivePhaseKey = PHASE_TO_STEP[phase] ?? phase;
  const currentPhaseIdx = PHASE_STEPS.findIndex((s) => s.key === effectivePhaseKey);
  const failedStepKey = inferFailedStepKey(phase, stepsData, output, customStatus);
  const failedStepIdx = failedStepKey
    ? PHASE_STEPS.findIndex((s) => s.key === failedStepKey)
    : -1;

  const getStepState = (idx: number, key: string): StepState => {
    if (phase === "completed") return "done";
    if (FAILURE_PHASES.has(phase)) {
      if (failedStepIdx >= 0) {
        if (idx < failedStepIdx) return "done";
        if (idx === failedStepIdx) return "error";
        return "skipped";
      }
      if (stepHasFailure(stepsData[key])) return "error";
      if (stepHasEvidence(stepsData[key])) return "done";
      return "skipped";
    }
    if (currentPhaseIdx < 0) return "pending";
    if (idx < currentPhaseIdx) return "done";
    if (idx === currentPhaseIdx) return "active";
    return "pending";
  };

  const getStepLog = (key: string, state: StepState): string | null => {
    if (state === "pending") return null;
    if (state === "skipped") return "⊘ Skipped — previous step failed.";
    const sd = stepsData[key] || {};
    const failureText =
      state === "error" ? getFailureText(sd, output, customStatus, job) : "";
    const stepLabel = PHASE_STEPS.find((step) => step.key === key)?.label ?? "Step";
    switch (key) {
      case "checking_vm": {
        const ps = sd.power_state as string;
        const started = sd.started as boolean;
        const vmLog = ((sd.output as string) || (sd.last_output as string) || "").trim();
        if (state === "error") return `✗ ${stepLabel} failed:\n${failureText}`;
        if (vmLog) return `${state === "done" ? "✓ VM ready." : "Checking VM..."}\n\n--- VM Check Log ---\n${vmLog}`;
        if (state === "done")
          return started
            ? `✓ VM was deallocated → started (power: ${ps || "running"}). Waited 30s for boot.`
            : `✓ VM already running (power: ${ps || "running"}).`;
        return "Checking Terminal sidecar reachability...";
      }
      case "enabling_storage":
        if (state === "error") return `✗ ${stepLabel} failed:\n${failureText}`;
        if (sd.output || sd.last_output) {
          return `${state === "done" ? "✓ Storage access configured." : "Configuring storage access..."}\n\n--- Storage Log ---\n${((sd.output as string) || (sd.last_output as string)).trim()}`;
        }
        return state === "done"
          ? "✓ Storage access configured for data transfer."
          : "Configuring storage network access...";
      case "uploading": {
        const bp = sd.blob_path as string;
        const uploadLog = ((sd.output as string) || (sd.last_output as string) || "").trim();
        if (state === "error") return `✗ ${stepLabel} failed:\n${failureText}`;
        if (uploadLog) return `${state === "done" ? "✓ Query uploaded." : "Uploading query..."}\n\n--- Upload Log ---\n${uploadLog}`;
        if (sd.skipped) return "✓ Query already uploaded (no inline data).";
        if (state === "done" && bp) return `✓ Query uploaded → ${bp}`;
        return state === "done"
          ? `✓ Query uploaded to queries/${jobId}/input.fa`
          : "Uploading FASTA query sequence...";
      }
      case "configuring": {
        const cu = sd.config_url as string;
        const configLog = ((sd.output as string) || (sd.last_output as string) || "").trim();
        if (state === "error") return `✗ ${stepLabel} failed:\n${failureText}`;
        if (configLog) return `${state === "done" ? "✓ Config generated." : "Generating config..."}\n\n--- Config Log ---\n${configLog}`;
        return state === "done"
          ? `✓ Config generated and uploaded.\n   ${cu || `queries/${jobId}/elastic-blast.ini`}`
          : "Generating elastic-blast INI configuration...";
      }
      case "warming_up": {
        const wo = ((sd.output as string) || (sd.last_output as string) || "").trim();
        if (state === "error") return `✗ Warmup failed:\n${wo || failureText}`;
        if (state === "done" && sd.success)
          return `✓ Cluster warmed up — DB shards loaded on local SSD.\n${wo ? `\n--- Console Output ---\n${wo}` : ""}`;
        if (state === "done")
          return `✓ Warmup step completed.\n${wo ? `\n--- Console Output ---\n${wo}` : ""}`;
        return "Running elastic-blast prepare — downloading DB shards to node SSDs...";
      }
      case "submitting": {
        const so =
          (sd.output as string) ||
          (sd.last_output as string) ||
          ((output as Record<string, unknown>)?.error as string);
        const liveOutput = ((sd.last_output as string) || (sd.output as string) || "").trim();
        const submitJobName = sd.submit_job_name as string | undefined;
        const pollAttempt = sd.poll_attempt as number | undefined;
        if (state === "error") return `✗ Submit failed:\n${so || failureText}`;
        if (state === "done" && sd.output)
          return `✓ Submitted successfully.\n\n--- Console Output ---\n${sd.output as string}`;
        if (state === "active" && liveOutput) {
          const meta = [
            submitJobName ? `helper job : ${submitJobName}` : null,
            pollAttempt ? `log poll   : #${pollAttempt}` : null,
          ].filter(Boolean).join("\n  ");
          return `Running elastic-blast submit...${meta ? `\n\n  ${meta}` : ""}\n\n--- Live Console Output ---\n${liveOutput}`;
        }
        return state === "done"
          ? "✓ Job submitted to AKS cluster."
          : "Starting elastic-blast submit helper job...";
      }
      case "running": {
        const blastStatus = customStatus?.blast_status as string;
        const pollAttempt = customStatus?.poll_attempt as number;
        const rd = sd as Record<string, unknown>;
        if (state === "active" && blastStatus) {
          const liveOutput = (rd.last_output as string | undefined)?.trim();
          return `Polling elastic-blast status...\n\n  BLAST status : ${blastStatus}\n  Poll attempt : #${pollAttempt ?? "?"}  (~${(pollAttempt ?? 0) * 30}s elapsed)${liveOutput ? `\n\n--- Live Status Output ---\n${liveOutput}` : ""}`;
        }
        if (state === "error") return `✗ BLAST run failed:\n${failureText}`;
        if (state === "done") {
          const polls = rd.polls as number;
          const lo = rd.last_output as string;
          let msg = `✓ BLAST completed after ${polls ?? "?"} polls (~${(polls ?? 0) * 30}s).`;
          if (lo) msg += `\n\n--- Last Status Output ---\n${lo}`;
          return msg;
        }
        return "Waiting for BLAST search to complete...";
      }
      case "exporting_results": {
        const ed = sd as Record<string, unknown>;
        const eo = ed.output as string;
        const liveExport = ed.last_output as string | undefined;
        const hasOut = ed.has_output_files as boolean | undefined;
        const verifyData = stepsData.result_verification as
          | Record<string, unknown>
          | undefined;
        const verifyAttempts = verifyData?.verify_attempts as number | undefined;
        const outInfo =
          hasOut !== undefined
            ? hasOut
              ? "✓ .out result files found in blob."
              : "⚠ No .out result files detected yet."
            : "";
        const verifyInfo = verifyAttempts
          ? ` (${verifyAttempts} verification polls)`
          : "";
        if (state === "error") return `✗ Export failed:\n${eo || failureText}`;
        if (state === "done" && ed.success)
          return `✓ Results exported.${verifyInfo}\n${outInfo}\n\n--- Export Log ---\n${eo || "(no output)"}`;
        if (state === "done" && ed.auth_failed)
          return `⚠ Export partially failed: VM az login expired.\n${outInfo}\nResults written by AKS pods directly may still be available.\n\n--- Export Log ---\n${eo || ""}`;
        if (state === "done")
          return `✓ Export step completed.${verifyInfo}\n${outInfo}${eo ? `\n\n--- Export Log ---\n${eo}` : ""}`;
        if (verifyAttempts)
          return `Verifying result blobs... (attempt ${verifyAttempts})${liveExport ? `\n\n--- Export Verification Log ---\n${liveExport}` : ""}`;
        if (state === "active" && liveExport)
          return `Waiting for results-export K8s job + capturing pod logs...\n\n--- Export Verification Log ---\n${liveExport}`;
        return "Waiting for results-export K8s job + capturing pod logs...";
      }
      case "completed": {
        if (state === "error") return `✗ Completion failed:\n${failureText}`;
        const totalPolls = (stepsData.running?.polls as number) || 0;
        return `✓ All steps completed.\n\n  Total polling time: ~${totalPolls * 30}s\n  Results container: results/${jobId}/`;
      }
      default:
        return null;
    }
  };

  const renderStepExtra = (key: string, state: StepState, isOpen: boolean) => {
    if (!isOpen || state === "pending") return null;
    // Upload: show input.fa with 1000 char limit (FASTA can be very large)
    if (
      key === "uploading" &&
      (state === "done" || state === "active") &&
      jobId &&
      subscriptionId &&
      storageAccount
    ) {
      return (
        <FilePreview
          jobId={jobId}
          filename="input.fa"
          subscriptionId={subscriptionId}
          storageAccount={storageAccount}
          maxBytes={1000}
        />
      );
    }
    // Configure: show full config (INI files are small)
    if (
      key === "configuring" &&
      (state === "done" || state === "active") &&
      jobId &&
      subscriptionId &&
      storageAccount
    ) {
      return (
        <FilePreview
          jobId={jobId}
          filename="elastic-blast.ini"
          subscriptionId={subscriptionId}
          storageAccount={storageAccount}
          maxBytes={10000}
        />
      );
    }
    return null;
  };

  return (
    <>
      <style>{shimmerStyle}</style>
      <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
        {PHASE_STEPS.map((s, i) => {
          const state = getStepState(i, s.key);
          const isOpen = expanded[s.key] ?? (state === "active" || state === "error");
          const log = getStepLog(s.key, state);
          const Icon = s.icon;

          const stateColor =
            state === "done"
              ? "var(--success)"
              : state === "active"
                ? "var(--accent)"
                : state === "error"
                  ? "var(--danger)"
                  : "var(--text-faint)";

          return (
            <div
              key={s.key}
              style={{
                borderRadius: 6,
                overflow: "hidden",
                background: isOpen ? "rgba(255,255,255,0.03)" : "transparent",
                border: isOpen ? "1px solid var(--border-weak)" : "1px solid transparent",
                opacity: state === "skipped" ? 0.5 : 1,
                position: "relative",
              }}
            >
              {/* Shimmer bar at top of active step */}
              {state === "active" && (
                <div
                  style={{
                    position: "absolute",
                    top: 0,
                    left: 0,
                    right: 0,
                    height: 2,
                    overflow: "hidden",
                    borderRadius: "6px 6px 0 0",
                    pointerEvents: "none",
                    background: "rgba(122,167,255,0.10)",
                  }}
                >
                  <div
                    style={{
                      position: "absolute",
                      top: 0,
                      left: 0,
                      width: "33%",
                      height: "100%",
                      background:
                        "linear-gradient(90deg, transparent 0%, var(--accent) 50%, transparent 100%)",
                      animation: "step-shimmer 1.2s linear infinite",
                    }}
                  />
                </div>
              )}
              <button
                onClick={() =>
                  state !== "pending" && state !== "skipped" && toggle(s.key)
                }
                disabled={state === "pending" || state === "skipped"}
                style={{
                  all: "unset",
                  display: "flex",
                  alignItems: "center",
                  gap: 10,
                  width: "100%",
                  padding: "8px 12px",
                  cursor: state === "pending" ? "default" : "pointer",
                  fontSize: 13,
                  boxSizing: "border-box",
                }}
              >
                {/* Expand chevron */}
                <span style={{ color: "var(--text-faint)", width: 14, flexShrink: 0 }}>
                  {state !== "pending" && state !== "skipped" ? (
                    isOpen ? (
                      <ChevronDown size={14} />
                    ) : (
                      <ChevronRight size={14} />
                    )
                  ) : null}
                </span>
                {/* Status icon */}
                <span style={{ flexShrink: 0 }}>
                  {state === "done" && (
                    <CheckCircle2 size={16} style={{ color: "var(--success)" }} />
                  )}
                  {state === "active" && (
                    <Loader2
                      size={16}
                      className="spin"
                      style={{ color: "var(--accent)" }}
                    />
                  )}
                  {state === "error" && (
                    <XCircle size={16} style={{ color: "var(--danger)" }} />
                  )}
                  {state === "skipped" && (
                    <Icon
                      size={15}
                      style={{ color: "var(--text-faint)", opacity: 0.5 }}
                    />
                  )}
                  {state === "pending" && (
                    <Icon
                      size={15}
                      style={{ color: "var(--text-faint)", opacity: 0.4 }}
                    />
                  )}
                </span>
                {/* Label */}
                <span
                  style={{
                    flex: 1,
                    color: stateColor,
                    fontWeight: state === "active" ? 600 : 400,
                  }}
                >
                  {s.label}
                  <span
                    style={{
                      fontSize: 11,
                      marginLeft: 8,
                      color: "var(--text-faint)",
                      fontWeight: 400,
                    }}
                  >
                    {state === "skipped" ? "Skipped" : s.desc}
                  </span>
                </span>
                {/* Duration + Status badge */}
                {(() => {
                  const dur = getStepDuration(s.key, state);
                  return (
                    <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
                      {dur && (
                        <span
                          style={{
                            fontSize: 11,
                            color:
                              state === "active" ? "var(--accent)" : "var(--text-faint)",
                            fontVariantNumeric: "tabular-nums",
                            fontWeight: state === "active" ? 500 : 400,
                            minWidth: 28,
                            textAlign: "right",
                          }}
                        >
                          {dur}
                        </span>
                      )}
                      {state === "done" && (
                        <span style={{ fontSize: 11, color: "var(--success)" }}>✓</span>
                      )}
                      {state === "skipped" && (
                        <span
                          style={{
                            fontSize: 10,
                            color: "var(--text-faint)",
                            padding: "1px 6px",
                            background: "rgba(255,255,255,0.04)",
                            borderRadius: 3,
                          }}
                        >
                          skipped
                        </span>
                      )}
                      {state === "error" && (
                        <span
                          style={{
                            fontSize: 10,
                            color: "var(--danger)",
                            padding: "1px 6px",
                            background: "rgba(224,123,138,0.08)",
                            borderRadius: 3,
                          }}
                        >
                          failed
                        </span>
                      )}
                    </span>
                  );
                })()}
              </button>
              {/* Collapsible log — premium CI-style */}
              {isOpen && log && <StepLogBlock log={log} state={state} stepKey={s.key} />}
              {/* Extra content: file previews */}
              {isOpen && renderStepExtra(s.key, state, isOpen) && (
                <div
                  style={{
                    padding: "8px 12px 10px 50px",
                    borderTop: log ? "none" : "1px solid var(--border-weak)",
                    background: "rgba(0,0,0,0.15)",
                  }}
                >
                  {renderStepExtra(s.key, state, isOpen)}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </>
  );
}
