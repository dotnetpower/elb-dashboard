import { Link } from "react-router-dom";
import { Loader2, XCircle } from "lucide-react";

import { BlastResultsTable, NoResultFilesPanel } from "./BlastResultsTable";
import { StorageLockedPanel } from "./StorageLockedPanel";
import type { splitBlastResultFiles } from "@/pages/blastResultsModel";

type ResultFile = ReturnType<typeof splitBlastResultFiles>["files"][number];

export interface ResultsBodyProps {
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
  supportFiles: ReturnType<typeof splitBlastResultFiles>["supportFiles"];
  debugFiles: ReturnType<typeof splitBlastResultFiles>["debugFiles"];
  hasOnlyDebugFiles: boolean;
  downloadingFile: string | null;
  terminalSidecarHealthy: boolean;
  hasRunningCluster: boolean;
  hasAnyCluster: boolean;
  onRetry: () => void;
  onDownload: (file: ResultFile) => void;
  onUnlocked: () => void;
}

export function ResultsBody({
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
  supportFiles,
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
          <strong style={{ color: "var(--danger)" }}>Search failed</strong> during
          the <strong>{failedStepLabel}</strong> step. Open the Run details tab for
          diagnostics.
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
  if (files.length > 0) {
    return (
      <BlastResultsTable
        files={files}
        resultFiles={resultFiles}
        supportFiles={supportFiles}
        debugFiles={debugFiles}
        hasOnlyDebugFiles={hasOnlyDebugFiles}
        downloadingFile={downloadingFile}
        onDownload={onDownload}
      />
    );
  }
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
          Output files will appear here once the search completes. Current phase:{" "}
          <strong style={{ color: "var(--accent)" }}>{effectivePhase}</strong>
        </span>
      </div>
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
        Output files will appear here once the search completes.
      </p>
    );
  }
}
