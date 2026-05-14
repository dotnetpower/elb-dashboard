import { useState, useEffect, useRef } from "react";
import { useParams, useSearchParams, Link } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Download,
  ArrowLeft,
  RefreshCw,
  Copy,
  CheckCircle2,
  Loader2,
  Server,
  Send,
  Trophy,
  Clock,
  XCircle,
  FileText,
  AlertTriangle,
  Unlock,
  FolderOpen,
  StopCircle,
  BarChart3,
} from "lucide-react";

import { blastApi } from "@/api/endpoints";
import { api } from "@/api/client";
import { loadSavedConfig } from "@/components/SetupWizard";
import { useToast } from "@/components/Toast";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import {
  PHASE_MESSAGES,
  FAILURE_PHASES,
  firstErrorLine,
  getFailureText,
  StepLogSection,
} from "@/components/BlastStepTimeline";
import { ElapsedTimer, formatBytes } from "@/components/BlastFilePreview";
import { useBlastResultActions } from "@/hooks/useBlastResultActions";
import {
  resolveBlastJobPhase,
  resolveBlastResultState,
  splitBlastResultFiles,
} from "@/pages/blastResultsModel";

function StorageLockedPanel({
  subscriptionId,
  storageAccount,
  resourceGroup,
  jobId,
  onUnlocked,
}: {
  subscriptionId: string;
  storageAccount: string;
  resourceGroup: string;
  jobId: string;
  onUnlocked: () => void;
}) {
  const { toast } = useToast();
  const resultsUrl = `https://${storageAccount}.blob.core.windows.net/results/${jobId}`;

  const enableMutation = useMutation({
    mutationFn: () =>
      api.post<{ public_network_access: string | null }>(
        "/monitor/storage/public-access",
        {
          subscription_id: subscriptionId,
          resource_group: resourceGroup,
          account_name: storageAccount,
          enabled: true,
        },
      ),
    onSuccess: () => {
      toast("Storage unlocked. Loading results...", "success");
      // Wait for propagation then refresh
      setTimeout(onUnlocked, 8000);
    },
    onError: (e) => toast(`Failed to enable storage: ${(e as Error).message}`, "error"),
  });

  return (
    <div style={{ marginTop: "var(--space-3)" }}>
      {/* Warning + action */}
      <div
        style={{
          padding: "16px",
          borderRadius: 10,
          background: "rgba(240,198,116,0.06)",
          border: "1px solid rgba(240,198,116,0.18)",
        }}
      >
        <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
          <AlertTriangle
            size={18}
            style={{ color: "var(--warning)", flexShrink: 0, marginTop: 2 }}
          />
          <div style={{ flex: 1 }}>
            <div
              style={{
                fontSize: 14,
                fontWeight: 600,
                color: "var(--warning)",
                marginBottom: 6,
              }}
            >
              Storage public access is disabled
            </div>
            <div style={{ fontSize: 12, color: "var(--text-muted)", lineHeight: 1.5 }}>
              Result files are stored in your Azure Blob Storage but cannot be listed
              while public access is off. Temporarily enable it to view and download your
              BLAST results.
            </div>
            <button
              className="glass-button glass-button--primary"
              onClick={() => enableMutation.mutate()}
              disabled={enableMutation.isPending}
              style={{
                marginTop: 12,
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                fontSize: 13,
              }}
            >
              {enableMutation.isPending ? (
                <>
                  <Loader2 size={14} className="spin" /> Enabling...
                </>
              ) : (
                <>
                  <Unlock size={14} strokeWidth={1.5} /> Enable Storage &amp; Load Results
                </>
              )}
            </button>
          </div>
        </div>
      </div>

      {/* Results location info */}
      <div
        style={{
          marginTop: "var(--space-3)",
          padding: "14px 16px",
          borderRadius: 10,
          background: "var(--bg-tertiary)",
          fontSize: 12,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
          <FolderOpen
            size={14}
            strokeWidth={1.5}
            style={{ color: "var(--text-muted)" }}
          />
          <span style={{ fontWeight: 600, color: "var(--text-primary)" }}>
            Results Location
          </span>
        </div>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "100px 1fr",
            gap: "4px 12px",
            color: "var(--text-muted)",
          }}
        >
          <span>Account</span>
          <code style={{ fontSize: 11, color: "var(--text-primary)" }}>
            {storageAccount}
          </code>
          <span>Container</span>
          <code style={{ fontSize: 11, color: "var(--text-primary)" }}>results</code>
          <span>Prefix</span>
          <code style={{ fontSize: 11, color: "var(--text-primary)" }}>{jobId}/</code>
          <span>URL</span>
          <code
            style={{ fontSize: 10, color: "var(--text-faint)", wordBreak: "break-all" }}
          >
            {resultsUrl}
          </code>
        </div>
      </div>
    </div>
  );
}

export function BlastResults() {
  const { jobId } = useParams<{ jobId: string }>();
  const [searchParams] = useSearchParams();
  const config = loadSavedConfig();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [showCancelConfirm, setShowCancelConfirm] = useState(false);
  const prevPhaseRef = useRef<string | null>(null);
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

  // Track phase transitions for toast notifications.
  // Only toast when we observe a LIVE transition (running → completed).
  // If the job is already terminal on first load, never toast.
  const initialPhaseRef = useRef<string | null>(null);
  useEffect(() => {
    if (!job) return;
    if (prevPhaseRef.current === null) {
      // First render — record phase, never toast
      prevPhaseRef.current = phase;
      initialPhaseRef.current = phase;
      return;
    }
    // If the job was already terminal when we first loaded, skip all toasts
    const wasTerminalOnLoad =
      initialPhaseRef.current === "completed" ||
      initialPhaseRef.current === "failed" ||
      initialPhaseRef.current === "error" ||
      initialPhaseRef.current === "submit_failed" ||
      initialPhaseRef.current === "warmup_failed" ||
      initialPhaseRef.current === "cancelled";
    if (wasTerminalOnLoad) {
      prevPhaseRef.current = phase;
      return;
    }
    if (phase && phase !== prevPhaseRef.current) {
      if (phase === "completed") toast("BLAST job completed successfully!", "success");
      else if (FAILURE_PHASES.has(phase)) toast("BLAST job failed.", "error");
    }
    prevPhaseRef.current = phase;
  }, [job, phase, toast]);

  return (
    <div className="page-stack">
      <header>
        <Link
          to="/blast/jobs"
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: "var(--space-2)",
            fontSize: 13,
            marginBottom: "var(--space-3)",
          }}
        >
          <ArrowLeft size={14} strokeWidth={1.5} /> All jobs
        </Link>
        <div style={{ display: "flex", alignItems: "center", gap: "var(--space-3)" }}>
          <h1 style={{ margin: 0, flex: 1 }}>{job?.job_title || jobId}</h1>
          {job?.created_at && isRunning && (
            <span
              style={{
                fontSize: 12,
                color: "var(--text-muted)",
                display: "inline-flex",
                alignItems: "center",
                gap: 4,
              }}
            >
              <Clock size={12} strokeWidth={1.5} />
              <ElapsedTimer startTime={job.created_at} />
            </span>
          )}
          {isRunning && (
            <button
              className="glass-button"
              onClick={() => setShowCancelConfirm(true)}
              disabled={cancelMutation.isPending}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 5,
                fontSize: 12,
                color: "var(--danger)",
              }}
            >
              <StopCircle size={14} strokeWidth={1.5} /> Cancel
            </button>
          )}
        </div>
      </header>

      {/* Job Info */}
      <section className="glass-card glass-card--strong">
        <h3 style={{ marginTop: 0 }}>Job Details</h3>
        {!job && (
          <div
            style={{ display: "flex", alignItems: "center", gap: 8 }}
            className="muted"
          >
            <Loader2 size={14} className="spin" /> Loading job details...
          </div>
        )}
        {job && (
          <>
            {/* Live status banner */}
            {isRunning && (
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
                    marginBottom: blastStatus || job?.created_at ? 8 : 0,
                  }}
                >
                  <Loader2
                    size={16}
                    className="spin"
                    style={{ color: "var(--accent)" }}
                  />
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
                      <strong style={{ color: "var(--text-primary)" }}>
                        {blastStatus}
                      </strong>
                    </span>
                    {pollAttempt != null && (
                      <span>
                        Poll #{pollAttempt} · ~{pollAttempt * 30}s elapsed
                      </span>
                    )}
                  </div>
                )}
                {job?.created_at && (
                  <div
                    style={{
                      fontSize: 11,
                      marginLeft: 26,
                      marginTop: 4,
                      color: "var(--text-faint)",
                    }}
                  >
                    Started {new Date(job.created_at).toLocaleString()}
                    {runtimeStatus && <> · Orchestrator: {runtimeStatus}</>}
                    {" · Elapsed: "}
                    <ElapsedTimer startTime={job.created_at} />
                  </div>
                )}
              </div>
            )}

            {/* Success banner — only when truly clean */}
            {phase === "completed" && !completedButFailed && !effectiveIsFailed && (
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
                    {resultFiles.length > 0
                      ? `${resultFiles.length} result file${resultFiles.length === 1 ? "" : "s"} ready for download`
                      : hasOnlyDebugFiles
                        ? "No BLAST results — diagnostic logs only"
                        : "Checking results..."}
                  </div>
                </div>
              </div>
            )}

            {/* Failure banner */}
            {effectiveIsFailed &&
              (() => {
                const failedStepData = failedStepKey
                  ? stepsObj[failedStepKey]
                  : undefined;
                const errorOutput = completedButFailed
                  ? submitOutput
                  : getFailureText(
                      failedStepData,
                      output,
                      customStatus,
                      job as unknown as Record<string, unknown>,
                    );
                const errorSummary =
                  firstErrorLine(errorOutput) || errorOutput.split("\n")[0] || "";

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
                        <div
                          style={{
                            fontSize: 15,
                            fontWeight: 600,
                            color: "var(--danger)",
                          }}
                        >
                          Job Failed at {failedStepLabel}
                        </div>
                        <div
                          style={{
                            fontSize: 12,
                            color: "var(--text-muted)",
                            marginTop: 2,
                          }}
                        >
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
              })()}

            <div
              style={{
                display: "grid",
                gridTemplateColumns: "140px 1fr",
                gap: "var(--space-2) var(--space-4)",
                fontSize: 13,
              }}
            >
              <span className="muted">Job ID</span>
              <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <code className="code-val">{job.job_id}</code>
                <button
                  className={`copy-btn${copiedId ? " copy-btn--copied" : ""}`}
                  onClick={copyJobId}
                  title="Copy Job ID"
                >
                  {copiedId ? <CheckCircle2 size={12} /> : <Copy size={12} />}
                </button>
              </span>
              <span className="muted">Program</span>
              <span>{job.program}</span>
              <span className="muted">Database</span>
              <span style={{ wordBreak: "break-all" }}>{job.db}</span>
              <span className="muted">Status</span>
              <span
                style={{ display: "flex", alignItems: "center", gap: "var(--space-2)" }}
              >
                <span
                  style={{
                    width: 8,
                    height: 8,
                    borderRadius: 999,
                    background: effectiveColor,
                    boxShadow: `0 0 8px ${effectiveColor}`,
                  }}
                />
                {effectivePhase === "submit_failed" ? "failed" : effectivePhase}
              </span>
              <span className="muted">Created</span>
              <span>
                {job.created_at ? new Date(job.created_at).toLocaleString() : "—"}
              </span>
              <span className="muted">Duration</span>
              <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <Clock
                  size={12}
                  strokeWidth={1.5}
                  style={{ color: "var(--text-faint)" }}
                />
                {job.created_at && isRunning ? (
                  <ElapsedTimer startTime={job.created_at} />
                ) : job.created_at && job.updated_at ? (
                  (() => {
                    const ms =
                      new Date(job.updated_at as string).getTime() -
                      new Date(job.created_at).getTime();
                    const s = Math.floor(ms / 1000);
                    const m = Math.floor(s / 60);
                    const h = Math.floor(m / 60);
                    return h > 0
                      ? `${h}h ${m % 60}m ${s % 60}s`
                      : m > 0
                        ? `${m}m ${s % 60}s`
                        : `${s}s`;
                  })()
                ) : (
                  "—"
                )}
              </span>
              {job.config_snapshot && (
                <>
                  <span className="muted">E-value</span>
                  <span>
                    {String(
                      (job.config_snapshot as Record<string, unknown>).evalue ?? "—",
                    )}
                  </span>
                  <span className="muted">Max targets</span>
                  <span>
                    {String(
                      (job.config_snapshot as Record<string, unknown>).max_target_seqs ??
                        "—",
                    )}
                  </span>
                  <span className="muted">Machine</span>
                  <span>
                    {String(
                      (job.config_snapshot as Record<string, unknown>).machine_type ??
                        "—",
                    )}
                  </span>
                  <span className="muted">Nodes</span>
                  <span>
                    {String(
                      (job.config_snapshot as Record<string, unknown>).num_nodes ?? "—",
                    )}
                  </span>
                </>
              )}
              {/* Infrastructure info */}
              {job.infrastructure &&
                (() => {
                  const infra = job.infrastructure as Record<string, unknown>;
                  return (
                    <>
                      <span className="muted">Cluster</span>
                      <span>
                        <code style={{ fontSize: 11 }}>
                          {String(infra.cluster_name ?? "—")}
                        </code>
                      </span>
                      <span className="muted">Region</span>
                      <span>{String(infra.region ?? "—")}</span>
                      <span className="muted">Resource Group</span>
                      <span>{String(infra.resource_group ?? "—")}</span>
                      <span className="muted">Storage</span>
                      <span>{String(infra.storage_account ?? "—")}</span>
                    </>
                  );
                })()}
            </div>
          </>
        )}

        {/* Summary metric cards for completed jobs */}
        {job && phase === "completed" && !effectiveIsFailed && files.length > 0 && (
          <>
            <div className="metric-grid" style={{ marginTop: "var(--space-3)" }}>
              <div className="metric-block">
                <div className="mv">{files.length}</div>
                <div className="mu">Result files</div>
              </div>
              <div className="metric-block">
                <div className="mv">
                  {formatBytes(files.reduce((sum, f) => sum + (f.size || 0), 0))}
                </div>
                <div className="mu">Total size</div>
              </div>
              <div className="metric-block">
                <div
                  className="mv"
                  style={{
                    color: completedButFailed ? "var(--danger)" : "var(--success)",
                  }}
                >
                  {completedButFailed ? (
                    <>
                      <XCircle
                        size={18}
                        style={{
                          display: "inline",
                          verticalAlign: "middle",
                          marginRight: 4,
                        }}
                      />
                      failed
                    </>
                  ) : (
                    <>
                      <CheckCircle2
                        size={18}
                        style={{
                          display: "inline",
                          verticalAlign: "middle",
                          marginRight: 4,
                        }}
                      />
                      {phase}
                    </>
                  )}
                </div>
                <div className="mu">Status</div>
              </div>
            </div>
            <div
              style={{
                marginTop: "var(--space-3)",
                display: "flex",
                gap: 8,
                flexWrap: "wrap",
              }}
            >
              <Link
                to={`/blast/jobs/${jobId}/analytics`}
                className="btn btn--primary btn--sm"
                style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
              >
                <BarChart3 size={14} strokeWidth={1.5} /> View Analytics &amp; Alignments
              </Link>
              {subscriptionId && storageAccount && (
                <>
                  <button
                    type="button"
                    onClick={() => handleExport("csv")}
                    disabled={exportingFormat !== null}
                    className="btn btn--ghost btn--sm"
                    style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
                  >
                    {exportingFormat === "csv" ? (
                      <Loader2 size={12} className="spin" />
                    ) : (
                      <Download size={12} />
                    )}
                    CSV
                  </button>
                  <button
                    type="button"
                    onClick={() => handleExport("json")}
                    disabled={exportingFormat !== null}
                    className="btn btn--ghost btn--sm"
                    style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
                  >
                    {exportingFormat === "json" ? (
                      <Loader2 size={12} className="spin" />
                    ) : (
                      <Download size={12} />
                    )}
                    JSON
                  </button>
                </>
              )}
            </div>
          </>
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

      {/* Step Logs — GitHub Actions style collapsible */}
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

      {/* Results */}
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
            onClick={() => resultsQuery.refetch()}
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

        {isRunning ? (
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
        ) : effectiveIsFailed ? (
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
        ) : !subscriptionId || !storageAccount ? (
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
        ) : publicAccessDisabled || resultsQuery.isError ? (
          <StorageLockedPanel
            subscriptionId={subscriptionId}
            storageAccount={storageAccount}
            resourceGroup={resourceGroup}
            jobId={jobId!}
            onUnlocked={() => {
              queryClient.invalidateQueries({ queryKey: ["blast-results"] });
              resultsQuery.refetch();
            }}
          />
        ) : files.length === 0 ? (
          <div style={{ marginTop: "var(--space-3)" }}>
            {phase === "completed" ? (
              <div
                style={{
                  padding: "16px",
                  borderRadius: 10,
                  background: "var(--bg-tertiary)",
                }}
              >
                <div
                  style={{ fontSize: 13, color: "var(--text-primary)", marginBottom: 10 }}
                >
                  No BLAST result files (.out) found in{" "}
                  <code style={{ fontSize: 12 }}>results/{jobId}/</code>.
                </div>
                <div
                  style={{ fontSize: 12, color: "var(--text-muted)", lineHeight: 1.6 }}
                >
                  This typically means the BLAST search returned no hits for the given
                  query and database combination.
                </div>
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                  <button
                    className="glass-button"
                    onClick={() => resultsQuery.refetch()}
                    style={{ fontSize: 12 }}
                  >
                    <RefreshCw size={13} /> Try Again
                  </button>
                  <Link
                    to="/terminal"
                    className="glass-button"
                    style={{ textDecoration: "none", fontSize: 12 }}
                  >
                    <Server size={13} /> Check Terminal
                  </Link>
                  <Link
                    to={`/blast/submit?resubmit=${encodeURIComponent(jobId!)}`}
                    className="glass-button glass-button--primary"
                    style={{ textDecoration: "none", fontSize: 12 }}
                  >
                    <Send size={13} /> Re-submit with Same Parameters
                  </Link>
                </div>
                {/* Results location */}
                <div
                  style={{
                    marginTop: 14,
                    paddingTop: 12,
                    borderTop: "1px solid var(--border-weak)",
                    display: "grid",
                    gridTemplateColumns: "80px 1fr",
                    gap: "3px 10px",
                    fontSize: 11,
                    color: "var(--text-faint)",
                  }}
                >
                  <span>Account</span>
                  <code style={{ fontSize: 11 }}>{storageAccount}</code>
                  <span>Container</span>
                  <code style={{ fontSize: 11 }}>results</code>
                  <span>Prefix</span>
                  <code style={{ fontSize: 11 }}>{jobId}/</code>
                </div>
              </div>
            ) : (
              <p className="muted">Results will appear here once the job completes.</p>
            )}
          </div>
        ) : (
          <div style={{ marginTop: "var(--space-3)" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead>
                <tr style={{ borderBottom: "1px solid var(--glass-border)" }}>
                  <th
                    style={{
                      textAlign: "left",
                      padding: "8px 12px",
                      color: "var(--text-muted)",
                      fontWeight: 500,
                      fontSize: 11,
                      textTransform: "uppercase",
                      letterSpacing: "0.06em",
                    }}
                  >
                    File
                  </th>
                  <th
                    style={{
                      textAlign: "right",
                      padding: "8px 12px",
                      color: "var(--text-muted)",
                      fontWeight: 500,
                      fontSize: 11,
                      textTransform: "uppercase",
                      letterSpacing: "0.06em",
                    }}
                  >
                    Size
                  </th>
                  <th
                    style={{
                      textAlign: "right",
                      padding: "8px 12px",
                      color: "var(--text-muted)",
                      fontWeight: 500,
                      fontSize: 11,
                      textTransform: "uppercase",
                      letterSpacing: "0.06em",
                    }}
                  >
                    Modified
                  </th>
                  <th style={{ width: 60 }} />
                </tr>
              </thead>
              <tbody>
                {files.map((f) => {
                  const fname = f.name.split("/").pop() || f.name;
                  const ext = fname.split(".").pop()?.toLowerCase() || "";
                  const isResult = ext === "out" || ext === "gz" || ext === "asn";
                  const isLog = ext === "log";
                  const typeColor = isResult
                    ? "var(--success)"
                    : isLog
                      ? "var(--warning)"
                      : "var(--text-faint)";
                  const typeLabel = isResult ? "RESULT" : isLog ? "LOG" : "INFO";
                  return (
                    <tr
                      key={f.name}
                      style={{ borderBottom: "1px solid var(--glass-border)" }}
                    >
                      <td
                        style={{
                          padding: "8px 12px",
                          display: "flex",
                          alignItems: "center",
                          gap: 8,
                        }}
                      >
                        <span
                          style={{
                            fontSize: 9,
                            padding: "1px 5px",
                            borderRadius: 3,
                            background: `color-mix(in srgb, ${typeColor} 15%, transparent)`,
                            color: typeColor,
                            fontWeight: 600,
                            letterSpacing: "0.04em",
                            flexShrink: 0,
                          }}
                        >
                          {typeLabel}
                        </span>
                        <code style={{ fontSize: 12 }}>{fname}</code>
                      </td>
                      <td
                        style={{ padding: "8px 12px", textAlign: "right" }}
                        className="muted"
                      >
                        {f.size != null ? formatBytes(f.size) : "—"}
                      </td>
                      <td
                        style={{ padding: "8px 12px", textAlign: "right" }}
                        className="muted"
                      >
                        {f.last_modified
                          ? new Date(f.last_modified).toLocaleString()
                          : "—"}
                      </td>
                      <td style={{ padding: "8px 12px", textAlign: "right" }}>
                        <button
                          className="glass-button"
                          onClick={() => handleDownload(f)}
                          disabled={downloadingFile === f.name}
                          title="Download"
                        >
                          {downloadingFile === f.name ? (
                            <Loader2 size={14} className="spin" />
                          ) : (
                            <Download size={14} strokeWidth={1.5} />
                          )}
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {hasOnlyDebugFiles && (
              <div
                style={{
                  marginTop: 12,
                  padding: "10px 14px",
                  borderRadius: 8,
                  background: "rgba(240,198,116,0.08)",
                  fontSize: 12,
                  color: "var(--text-muted)",
                }}
              >
                <AlertTriangle
                  size={13}
                  style={{
                    verticalAlign: "middle",
                    marginRight: 6,
                    color: "var(--warning)",
                  }}
                />
                No BLAST result files (.out) were produced. The files above are diagnostic
                logs from the cluster. This typically means the search returned no hits
                for the query/database combination.
              </div>
            )}
            {debugFiles.length > 0 && resultFiles.length > 0 && (
              <details style={{ marginTop: 14, fontSize: 12 }}>
                <summary style={{ cursor: "pointer", color: "var(--text-muted)" }}>
                  {debugFiles.length} diagnostic file{debugFiles.length > 1 ? "s" : ""}{" "}
                  (logs, status)
                </summary>
                <div style={{ marginTop: 8, display: "flex", flexWrap: "wrap", gap: 8 }}>
                  {debugFiles.map((f) => {
                    const fname = f.name.split("/").pop() || f.name;
                    return (
                      <button
                        key={f.name}
                        className="glass-button"
                        style={{ fontSize: 11, padding: "4px 10px" }}
                        onClick={() => handleDownload(f)}
                      >
                        <Download size={11} /> {fname}
                      </button>
                    );
                  })}
                </div>
              </details>
            )}
          </div>
        )}
      </section>

      {/* Cancel confirmation dialog */}
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
