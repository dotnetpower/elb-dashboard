import { CheckCircle2, Loader2, Trophy, XCircle } from "lucide-react";

import { ElapsedTimer } from "@/components/BlastFilePreview";
import {
  PHASE_MESSAGES,
  firstErrorLine,
  getFailureText,
} from "@/components/BlastStepTimeline";
import type { BlastJobSummary } from "@/api/endpoints";

interface RunningBannerProps {
  phase: string;
  blastStatus: string | undefined;
  pollAttempt: number | undefined;
  runtimeStatus: string | undefined;
  createdAt: string | null;
}

/** Blue gradient panel shown while the orchestrator is still working. */
export function BlastJobRunningBanner({
  phase,
  blastStatus,
  pollAttempt,
  runtimeStatus,
  createdAt,
}: RunningBannerProps) {
  return (
    <div
      style={{
        padding: "12px 16px",
        marginBottom: "var(--space-3)",
        borderRadius: 10,
        background:
          "linear-gradient(135deg, rgba(110,159,255,0.08), rgba(110,159,255,0.03))",
        border: "1px solid rgba(110,159,255,0.18)",
        fontSize: 13,
        color: "var(--text-primary)",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 10,
          marginBottom: blastStatus || createdAt ? 8 : 0,
        }}
      >
        <Loader2 size={16} className="spin" style={{ color: "var(--accent)" }} />
        <strong style={{ color: "var(--accent)", fontSize: 14 }}>
          {PHASE_MESSAGES[phase] ?? `Phase: ${phase}`}
        </strong>
      </div>
      {blastStatus && (
        <div
          style={{
            fontSize: 12,
            display: "flex",
            gap: 16,
            marginLeft: 26,
            color: "var(--text-muted)",
          }}
        >
          <span>
            BLAST:{" "}
            <strong style={{ color: "var(--text-primary)" }}>{blastStatus}</strong>
          </span>
          {pollAttempt != null && (
            <span>
              Poll #{pollAttempt} · ~{pollAttempt * 30}s elapsed
            </span>
          )}
        </div>
      )}
      {createdAt && (
        <div
          style={{
            fontSize: 11,
            marginLeft: 26,
            marginTop: 4,
            color: "var(--text-faint)",
          }}
        >
          Started {new Date(createdAt).toLocaleString()}
          {runtimeStatus && <> · Orchestrator: {runtimeStatus}</>}
          {" · Elapsed: "}
          <ElapsedTimer startTime={createdAt} />
        </div>
      )}
    </div>
  );
}

interface SuccessBannerProps {
  resultFileCount: number;
  hasOnlyDebugFiles: boolean;
}

/** Green panel shown when phase=completed AND nothing failed downstream. */
export function BlastJobSuccessBanner({
  resultFileCount,
  hasOnlyDebugFiles,
}: SuccessBannerProps) {
  return (
    <div
      style={{
        padding: "14px 16px",
        marginBottom: "var(--space-3)",
        borderRadius: 10,
        background:
          "linear-gradient(135deg, rgba(106,214,163,0.12), rgba(106,214,163,0.04))",
        border: "1px solid rgba(106,214,163,0.25)",
        display: "flex",
        alignItems: "center",
        gap: 12,
      }}
    >
      <div
        style={{
          width: 36,
          height: 36,
          borderRadius: "50%",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          background: "var(--success)",
          color: "#fff",
          boxShadow: "0 0 16px rgba(106,214,163,0.4)",
        }}
      >
        <Trophy size={18} />
      </div>
      <div>
        <div style={{ fontSize: 15, fontWeight: 600, color: "var(--success)" }}>
          Job Completed Successfully
        </div>
        <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 2 }}>
          {resultFileCount > 0
            ? `${resultFileCount} result file${resultFileCount === 1 ? "" : "s"} ready for download`
            : hasOnlyDebugFiles
              ? "No BLAST results — diagnostic logs only"
              : "Checking results..."}
        </div>
      </div>
    </div>
  );
}

interface FailureBannerProps {
  job: BlastJobSummary;
  effectivePhase: string;
  failedStepKey: string | null;
  failedStepLabel: string;
  stepsObj: Record<string, Record<string, unknown>>;
  output: Record<string, unknown> | null;
  customStatus: Record<string, unknown> | null;
  completedButFailed: boolean;
  submitOutput: string;
}

/** Red panel shown for any kind of failure (submit / runtime / completedButFailed). */
export function BlastJobFailureBanner({
  job,
  effectivePhase,
  failedStepKey,
  failedStepLabel,
  stepsObj,
  output,
  customStatus,
  completedButFailed,
  submitOutput,
}: FailureBannerProps) {
  const failedStepData = failedStepKey ? stepsObj[failedStepKey] : undefined;
  const errorOutput = completedButFailed
    ? submitOutput
    : getFailureText(
        failedStepData,
        output,
        customStatus,
        job as unknown as Record<string, unknown>,
      );
  const errorSummary = firstErrorLine(errorOutput) || errorOutput.split("\n")[0] || "";

  return (
    <div
      style={{
        padding: "14px 16px",
        marginBottom: "var(--space-3)",
        borderRadius: 10,
        background:
          "linear-gradient(135deg, rgba(224,123,138,0.12), rgba(224,123,138,0.04))",
        border: "1px solid rgba(224,123,138,0.25)",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <div
          style={{
            width: 36,
            height: 36,
            borderRadius: "50%",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            background: "var(--danger)",
            color: "#fff",
            flexShrink: 0,
          }}
        >
          <XCircle size={18} />
        </div>
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 15, fontWeight: 600, color: "var(--danger)" }}>
            Job Failed at {failedStepLabel}
          </div>
          <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 2 }}>
            {effectivePhase === "submit_failed"
              ? "The ElasticBLAST submit command failed. Check the execution steps below for details."
              : "An error occurred during execution. The failed step is expanded below when details are available."}
          </div>
        </div>
      </div>
      {errorSummary && (
        <div
          style={{
            marginTop: 10,
            padding: "8px 12px",
            borderRadius: 6,
            background: "rgba(224,123,138,0.06)",
            border: "1px solid rgba(224,123,138,0.12)",
            fontFamily: "var(--font-mono)",
            fontSize: 11,
            color: "var(--danger)",
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
          }}
        >
          {errorSummary}
        </div>
      )}
    </div>
  );
}

/** Marker icon used by the metric strip below the banners. */
export function BlastJobStatusIcon({
  completedButFailed,
}: {
  completedButFailed: boolean;
}) {
  return completedButFailed ? (
    <XCircle
      size={18}
      style={{ display: "inline", verticalAlign: "middle", marginRight: 4 }}
    />
  ) : (
    <CheckCircle2
      size={18}
      style={{ display: "inline", verticalAlign: "middle", marginRight: 4 }}
    />
  );
}
