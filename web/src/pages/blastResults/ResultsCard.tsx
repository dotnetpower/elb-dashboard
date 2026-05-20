import { FileText, RefreshCw } from "lucide-react";

import { ResultsBody } from "./ResultsBody";
import type { BlastResultsState } from "./useBlastResultsState";

export interface ResultsCardProps {
  jobId: string;
  state: BlastResultsState;
}

export function ResultsCard({ jobId, state }: ResultsCardProps) {
  const {
    resultsQuery,
    subscriptionId,
    storageAccount,
    resourceGroup,
    isRunning,
    effectiveIsFailed,
    effectivePhase,
    failedStepLabel,
    publicAccessDisabled,
    phase,
    files,
    resultFiles,
    supportFiles,
    debugFiles,
    hasOnlyDebugFiles,
    actions,
    terminalSidecar,
    cluster,
    queryClient,
    job,
  } = state;

  const refetchResults = resultsQuery.refetch;
  const manifest = resultsQuery.data?.manifest;
  const payload = job?.payload ?? {};
  const provenance = recordFrom(payload.provenance);
  const compatibility =
    recordFrom(provenance?.compatibility) ?? recordFrom(payload.compatibility_contract);
  const compatibilityMode = stringValue(compatibility?.mode);
  const blastVersion = stringValue(recordFrom(provenance?.blast)?.version);

  return (
    <section className="glass-card">
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
        }}
      >
        <h3
          style={{
            margin: 0,
            display: "flex",
            alignItems: "center",
            gap: 8,
          }}
        >
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

      {(manifest || compatibilityMode || blastVersion) && (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))",
            gap: 8,
            marginTop: "var(--space-3)",
            marginBottom: "var(--space-3)",
          }}
        >
          {compatibilityMode && (
            <ResultSummaryPill label="Compatibility" value={compatibilityMode} />
          )}
          {blastVersion && <ResultSummaryPill label="BLAST+" value={blastVersion} />}
          {manifest && (
            <ResultSummaryPill
              label="Manifest"
              value={`${manifest.status} · ${manifest.parseable_count}/${manifest.file_count}`}
            />
          )}
        </div>
      )}

      <ResultsBody
        jobId={jobId}
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
        supportFiles={supportFiles}
        debugFiles={debugFiles}
        hasOnlyDebugFiles={hasOnlyDebugFiles}
        downloadingFile={actions.downloadingFile}
        terminalSidecarHealthy={terminalSidecar.isHealthy}
        hasRunningCluster={cluster.hasRunningCluster}
        hasAnyCluster={cluster.hasAnyCluster}
        onRetry={() => refetchResults()}
        onDownload={actions.handleDownload}
        onUnlocked={() => {
          queryClient.invalidateQueries({ queryKey: ["blast-results"] });
          void refetchResults();
        }}
      />
    </section>
  );
}

function ResultSummaryPill({ label, value }: { label: string; value: string }) {
  return (
    <div
      style={{
        border: "1px solid rgba(148, 163, 184, 0.22)",
        borderRadius: 8,
        padding: "8px 10px",
        background: "rgba(15, 23, 42, 0.20)",
        minWidth: 0,
      }}
    >
      <div className="muted" style={{ fontSize: 11, marginBottom: 2 }}>
        {label}
      </div>
      <div style={{ fontSize: 13, fontWeight: 600, overflowWrap: "anywhere" }}>
        {value}
      </div>
    </div>
  );
}

function recordFrom(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}
