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
    debugFiles,
    hasOnlyDebugFiles,
    actions,
    terminalSidecar,
    cluster,
    queryClient,
  } = state;

  const refetchResults = resultsQuery.refetch;

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
