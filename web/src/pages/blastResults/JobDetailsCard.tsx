import { Link } from "react-router-dom";
import { Loader2, XCircle } from "lucide-react";

import {
  BlastJobFailureBanner,
  BlastJobRunningBanner,
  BlastJobSuccessBanner,
} from "./BlastJobBanners";
import { BlastJobDetailsGrid } from "./BlastJobDetailsGrid";
import { BlastJobMetrics } from "./BlastJobMetrics";
import type { BlastResultsState } from "./useBlastResultsState";

export interface JobDetailsCardProps {
  jobId: string;
  state: BlastResultsState;
}

/**
 * Encapsulates the entire "Job Details" glass-card — loading / error
 * placeholders, the three live banners (running / success / failure),
 * the details grid, completed metrics, and a generic error block. The
 * orchestrator passes the full state object so we don't enumerate dozens
 * of props.
 */
export function JobDetailsCard({ jobId, state }: JobDetailsCardProps) {
  const {
    job,
    jobQuery,
    isRunning,
    phase,
    blastStatus,
    pollAttempt,
    runtimeStatus,
    completedButFailed,
    effectiveIsFailed,
    effectivePhase,
    effectiveColor,
    resultFiles,
    hasOnlyDebugFiles,
    failedStepKey,
    failedStepLabel,
    stepsObj,
    output,
    customStatus,
    submitOutput,
    actions,
    showCompletedMetrics,
    hasExportTargets,
    files,
  } = state;

  return (
    <section className="glass-card glass-card--strong">
      <h3 style={{ marginTop: 0 }}>Job Details</h3>
      {!job && jobQuery.isLoading && (
        <div
          style={{ display: "flex", alignItems: "center", gap: 8 }}
          className="muted"
        >
          <Loader2 size={14} className="spin" /> Loading job details...
        </div>
      )}
      {!job && !jobQuery.isLoading && jobQuery.isError && (
        <div
          role="alert"
          style={{
            display: "flex",
            alignItems: "flex-start",
            gap: 8,
            padding: "var(--space-3)",
            borderRadius: "var(--radius-md)",
            border: "1px solid rgba(224,123,138,0.45)",
            background: "rgba(224,123,138,0.10)",
            color: "var(--text-primary)",
          }}
        >
          <XCircle
            size={16}
            style={{ color: "var(--danger)", flexShrink: 0, marginTop: 2 }}
          />
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <strong>Job not available</strong>
            <span className="muted" style={{ fontSize: 12 }}>
              {(jobQuery.error as Error)?.message ??
                "Unable to load job details."}
            </span>
            <Link to="/blast/jobs" style={{ fontSize: 12, marginTop: 4 }}>
              Back to job list
            </Link>
          </div>
        </div>
      )}
      {job && (
        <>
          {isRunning && (
            <BlastJobRunningBanner
              phase={phase}
              blastStatus={blastStatus}
              pollAttempt={pollAttempt}
              runtimeStatus={runtimeStatus}
              createdAt={job.created_at ?? null}
            />
          )}
          {phase === "completed" &&
            !completedButFailed &&
            !effectiveIsFailed && (
              <BlastJobSuccessBanner
                resultFileCount={resultFiles.length}
                hasOnlyDebugFiles={hasOnlyDebugFiles}
              />
            )}
          {effectiveIsFailed && (
            <BlastJobFailureBanner
              job={job}
              effectivePhase={effectivePhase}
              failedStepKey={failedStepKey}
              failedStepLabel={failedStepLabel}
              stepsObj={stepsObj}
              output={output}
              customStatus={customStatus}
              completedButFailed={completedButFailed}
              submitOutput={submitOutput}
            />
          )}
          <BlastJobDetailsGrid
            job={job}
            effectivePhase={effectivePhase}
            effectiveColor={effectiveColor}
            isRunning={isRunning}
            copiedId={actions.copiedId}
            onCopyJobId={actions.copyJobId}
          />
        </>
      )}

      {showCompletedMetrics && (
        <BlastJobMetrics
          jobId={jobId}
          files={files}
          phase={phase}
          completedButFailed={completedButFailed}
          hasExportTargets={hasExportTargets}
          exportingFormat={actions.exportingFormat}
          onExport={actions.handleExport}
        />
      )}

      {job?.error && phase !== "failed" && phase !== "error" && (
        <div
          style={{
            marginTop: "var(--space-4)",
            padding: "var(--space-3)",
            background: "rgba(224, 123, 138, 0.12)",
            borderRadius: 8,
            color: "var(--danger)",
            fontSize: 13,
          }}
        >
          {String(job.error)}
        </div>
      )}
    </section>
  );
}
