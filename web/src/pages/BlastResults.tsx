import { useEffect, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { FileText, Loader2, RefreshCw, XCircle } from "lucide-react";

import { blastApi } from "@/api/endpoints";
import {
  FAILURE_PHASES,
  StepLogSection,
} from "@/components/BlastStepTimeline";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { loadSavedConfig } from "@/components/SetupWizard";
import { useToast } from "@/components/Toast";
import { useBlastResultActions } from "@/hooks/useBlastResultActions";
import {
  useClusterReadiness,
  useTerminalSidecarHealth,
} from "@/hooks/usePrerequisites";
import {
  BlastJobFailureBanner,
  BlastJobRunningBanner,
  BlastJobSuccessBanner,
} from "@/pages/blastResults/BlastJobBanners";
import { BlastJobDetailsGrid } from "@/pages/blastResults/BlastJobDetailsGrid";
import { BlastJobHeader } from "@/pages/blastResults/BlastJobHeader";
import { BlastJobMetrics } from "@/pages/blastResults/BlastJobMetrics";
import {
  BlastResultsTable,
  NoResultFilesPanel,
} from "@/pages/blastResults/BlastResultsTable";
import { StorageLockedPanel } from "@/pages/blastResults/StorageLockedPanel";
import {
  resolveBlastJobPhase,
  resolveBlastResultState,
  splitBlastResultFiles,
} from "@/pages/blastResultsModel";

const TERMINAL_PHASES = new Set([
  "completed",
  "failed",
  "error",
  "submit_failed",
  "warmup_failed",
  "cancelled",
]);

export function BlastResults() {
  const { jobId } = useParams<{ jobId: string }>();
  const [searchParams] = useSearchParams();
  const config = loadSavedConfig();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [showCancelConfirm, setShowCancelConfirm] = useState(false);
  const cluster = useClusterReadiness();
  const terminalSidecar = useTerminalSidecarHealth();

  const subscriptionId =
    searchParams.get("subscription_id") || config?.subscriptionId || "";
  const storageAccount =
    searchParams.get("storage_account") || config?.storageAccountName || "";
  const resourceGroup = config?.workloadResourceGroup || "";

  const jobQuery = useQuery({
    queryKey: ["blast-job", jobId],
    queryFn: () => blastApi.getJob(jobId!),
    enabled: Boolean(jobId),
    refetchInterval: (q) => {
      const d = q.state.data;
      if (!d) return 3_000;
      if (
        d.status === "completed" ||
        d.status === "failed" ||
        d.runtime_status === "Completed" ||
        d.runtime_status === "Failed"
      )
        return false;
      return 5_000;
    },
  });

  const resultsQuery = useQuery({
    queryKey: ["blast-results", jobId, subscriptionId, storageAccount, resourceGroup],
    queryFn: () =>
      blastApi.listResults(jobId!, subscriptionId, storageAccount, resourceGroup),
    enabled: Boolean(jobId && subscriptionId && storageAccount),
    refetchInterval: (q) => {
      if (q.state.data?.files && q.state.data.files.length > 0) return false;
      if (q.state.data?.public_access_disabled) return false;
      return 30_000;
    },
  });
  const refetchResults = resultsQuery.refetch;

  const job = jobQuery.data;
  const allFiles = resultsQuery.data?.files ?? [];
  const { resultFiles, debugFiles, files, hasOnlyDebugFiles } =
    splitBlastResultFiles(allFiles);
  const publicAccessDisabled = resultsQuery.data?.public_access_disabled === true;

  const { customStatus, output, outputStatus, phase, isJobFailed } =
    resolveBlastJobPhase(job);
  const {
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
  } = resolveBlastResultState({
    job,
    phase,
    customStatus,
    output,
    outputStatus,
    isJobFailed,
  });

  const {
    copiedId,
    copyJobId,
    downloadingFile,
    exportingFormat,
    handleDownload,
    handleExport,
    cancelMutation,
  } = useBlastResultActions({ jobId, subscriptionId, storageAccount });

  // Phase transition toaster — only fire when we observe a LIVE transition
  // (running → terminal). Skip toasts when the job was already terminal on
  // first load. `prevPhaseRef` and `initialPhaseRef` are read+written inside
  // the effect; phase is the only meaningful trigger.
  const prevPhaseRef = useRef<string | null>(null);
  const initialPhaseRef = useRef<string | null>(null);
  useEffect(() => {
    if (!job) return;
    if (prevPhaseRef.current === null) {
      prevPhaseRef.current = phase;
      initialPhaseRef.current = phase;
      return;
    }
    if (initialPhaseRef.current && TERMINAL_PHASES.has(initialPhaseRef.current)) {
      prevPhaseRef.current = phase;
      return;
    }
    if (phase && phase !== prevPhaseRef.current) {
      if (phase === "completed") toast("BLAST job completed successfully!", "success");
      else if (FAILURE_PHASES.has(phase)) toast("BLAST job failed.", "error");
    }
    prevPhaseRef.current = phase;
  }, [job, phase, toast]);

  const hasExportTargets = Boolean(subscriptionId && storageAccount);
  const showCompletedMetrics =
    Boolean(job) && phase === "completed" && !effectiveIsFailed && files.length > 0;

  return (
    <div className="page-stack">
      <BlastJobHeader
        jobId={jobId!}
        jobTitle={job?.job_title ?? null}
        createdAt={job?.created_at ?? null}
        isRunning={isRunning}
        cancelDisabled={cancelMutation.isPending}
        onRequestCancel={() => setShowCancelConfirm(true)}
      />

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
            <XCircle size={16} style={{ color: "var(--danger)", flexShrink: 0, marginTop: 2 }} />
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <strong>Job not available</strong>
              <span className="muted" style={{ fontSize: 12 }}>
                {(jobQuery.error as Error)?.message ?? "Unable to load job details."}
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
            {phase === "completed" && !completedButFailed && !effectiveIsFailed && (
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
              copiedId={copiedId}
              onCopyJobId={copyJobId}
            />
          </>
        )}

        {showCompletedMetrics && (
          <BlastJobMetrics
            jobId={jobId!}
            files={files}
            phase={phase}
            completedButFailed={completedButFailed}
            hasExportTargets={hasExportTargets}
            exportingFormat={exportingFormat}
            onExport={handleExport}
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

      {job && (
        <section className="glass-card" style={{ padding: "14px 16px" }}>
          <h3
            style={{
              margin: "0 0 10px 0",
              fontSize: 14,
              display: "flex",
              alignItems: "center",
              gap: 8,
            }}
          >
            <FileText size={15} strokeWidth={1.5} /> Execution Steps
          </h3>
          <StepLogSection
            phase={effectivePhase}
            job={job as unknown as Record<string, unknown>}
            subscriptionId={subscriptionId}
            storageAccount={storageAccount}
          />
        </section>
      )}

      <section className="glass-card">
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
          }}
        >
          <h3 style={{ margin: 0, display: "flex", alignItems: "center", gap: 8 }}>
            <FileText size={16} strokeWidth={1.5} /> Results
          </h3>
          <button
            className="glass-button"
            onClick={() => refetchResults()}
            disabled={resultsQuery.isFetching}
            style={{ display: "flex", alignItems: "center", gap: 4 }}
          >
            <RefreshCw
              size={14}
              strokeWidth={1.5}
              className={resultsQuery.isFetching ? "spin" : ""}
            />{" "}
            Refresh
          </button>
        </div>

        <ResultsBody
          jobId={jobId!}
          subscriptionId={subscriptionId}
          storageAccount={storageAccount}
          resourceGroup={resourceGroup}
          isRunning={isRunning}
          effectiveIsFailed={effectiveIsFailed}
          effectivePhase={effectivePhase}
          failedStepLabel={failedStepLabel}
          publicAccessDisabled={publicAccessDisabled}
          resultsQueryIsError={resultsQuery.isError}
          phase={phase}
          files={files}
          resultFiles={resultFiles}
          debugFiles={debugFiles}
          hasOnlyDebugFiles={hasOnlyDebugFiles}
          downloadingFile={downloadingFile}
          terminalSidecarHealthy={terminalSidecar.isHealthy}
          hasRunningCluster={cluster.hasRunningCluster}
          hasAnyCluster={cluster.hasAnyCluster}
          onRetry={() => refetchResults()}
          onDownload={handleDownload}
          onUnlocked={() => {
            queryClient.invalidateQueries({ queryKey: ["blast-results"] });
            void refetchResults();
          }}
        />
      </section>

      <ConfirmDialog
        open={showCancelConfirm}
        title="Cancel BLAST Job"
        message={`Are you sure you want to cancel "${job?.job_title || jobId}"? This will terminate the running orchestrator. Any in-progress work on the AKS cluster may need manual cleanup.`}
        confirmLabel="Cancel Job"
        onConfirm={() => {
          setShowCancelConfirm(false);
          cancelMutation.mutate();
        }}
        onCancel={() => setShowCancelConfirm(false)}
      />
    </div>
  );
}

interface ResultsBodyProps {
  jobId: string;
  subscriptionId: string;
  storageAccount: string;
  resourceGroup: string;
  isRunning: boolean;
  effectiveIsFailed: boolean;
  effectivePhase: string;
  failedStepLabel: string;
  publicAccessDisabled: boolean;
  resultsQueryIsError: boolean;
  phase: string;
  files: ReturnType<typeof splitBlastResultFiles>["files"];
  resultFiles: ReturnType<typeof splitBlastResultFiles>["resultFiles"];
  debugFiles: ReturnType<typeof splitBlastResultFiles>["debugFiles"];
  hasOnlyDebugFiles: boolean;
  downloadingFile: string | null;
  terminalSidecarHealthy: boolean;
  hasRunningCluster: boolean;
  hasAnyCluster: boolean;
  onRetry: () => void;
  onDownload: (file: ReturnType<typeof splitBlastResultFiles>["files"][number]) => void;
  onUnlocked: () => void;
}

function ResultsBody({
  jobId,
  subscriptionId,
  storageAccount,
  resourceGroup,
  isRunning,
  effectiveIsFailed,
  effectivePhase,
  failedStepLabel,
  publicAccessDisabled,
  resultsQueryIsError,
  phase,
  files,
  resultFiles,
  debugFiles,
  hasOnlyDebugFiles,
  downloadingFile,
  terminalSidecarHealthy,
  hasRunningCluster,
  hasAnyCluster,
  onRetry,
  onDownload,
  onUnlocked,
}: ResultsBodyProps) {
  if (isRunning) {
    return (
      <div
        style={{
          marginTop: "var(--space-3)",
          padding: "16px",
          borderRadius: 10,
          background: "var(--bg-tertiary)",
          fontSize: 13,
          display: "flex",
          alignItems: "center",
          gap: 10,
        }}
      >
        <Loader2
          size={16}
          className="spin"
          style={{ color: "var(--accent)", flexShrink: 0 }}
        />
        <span style={{ color: "var(--text-muted)" }}>
          Results will appear here once the job completes. Current phase:{" "}
          <strong style={{ color: "var(--accent)" }}>{effectivePhase}</strong>
        </span>
      </div>
    );
  }
  if (effectiveIsFailed) {
    return (
      <div
        style={{
          marginTop: "var(--space-3)",
          padding: "16px",
          borderRadius: 10,
          background: "rgba(224,123,138,0.04)",
          border: "1px solid rgba(224,123,138,0.15)",
          fontSize: 13,
          display: "flex",
          alignItems: "center",
          gap: 10,
        }}
      >
        <XCircle size={16} style={{ color: "var(--danger)", flexShrink: 0 }} />
        <span style={{ color: "var(--text-muted)" }}>
          No results available — the job failed at the{" "}
          <strong style={{ color: "var(--danger)" }}>{failedStepLabel}</strong> step.
        </span>
      </div>
    );
  }
  if (!subscriptionId || !storageAccount) {
    return (
      <div className="muted" style={{ marginTop: "var(--space-3)" }}>
        <p>Cannot load results — missing Azure configuration.</p>
        <Link
          to="/"
          className="glass-button glass-button--primary"
          style={{
            textDecoration: "none",
            fontSize: 12,
            marginTop: 8,
            display: "inline-flex",
          }}
        >
          Configure on Dashboard →
        </Link>
      </div>
    );
  }
  if (publicAccessDisabled || resultsQueryIsError) {
    return (
      <StorageLockedPanel
        subscriptionId={subscriptionId}
        storageAccount={storageAccount}
        resourceGroup={resourceGroup}
        jobId={jobId}
        onUnlocked={onUnlocked}
      />
    );
  }
  if (files.length === 0) {
    if (phase === "completed") {
      return (
        <div style={{ marginTop: "var(--space-3)" }}>
          <NoResultFilesPanel
            jobId={jobId}
            storageAccount={storageAccount}
            terminalSidecarHealthy={terminalSidecarHealthy}
            hasRunningCluster={hasRunningCluster}
            hasAnyCluster={hasAnyCluster}
            onRetry={onRetry}
          />
        </div>
      );
    }
    return (
      <p className="muted" style={{ marginTop: "var(--space-3)" }}>
        Results will appear here once the job completes.
      </p>
    );
  }
  return (
    <BlastResultsTable
      files={files}
      resultFiles={resultFiles}
      debugFiles={debugFiles}
      hasOnlyDebugFiles={hasOnlyDebugFiles}
      downloadingFile={downloadingFile}
      onDownload={onDownload}
    />
  );
}

