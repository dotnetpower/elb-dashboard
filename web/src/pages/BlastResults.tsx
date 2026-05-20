import { useEffect, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";

import { ConfirmDialog } from "@/components/ConfirmDialog";
import { BlastJobHeader } from "@/pages/blastResults/BlastJobHeader";
import {
  BlastResultsTabs,
  resolveBlastResultsTab,
} from "@/pages/blastResults/BlastResultsTabs";
import { ExecutionStepsCard } from "@/pages/blastResults/ExecutionStepsCard";
import { JobDetailsCard } from "@/pages/blastResults/JobDetailsCard";
import { ResultsCard } from "@/pages/blastResults/ResultsCard";
import { AlignmentsTabBody } from "@/pages/blastResults/analytics/AlignmentsTabBody";
import { DescriptionsTabBody } from "@/pages/blastResults/analytics/DescriptionsTabBody";
import { GraphicSummaryPanel } from "@/pages/blastResults/analytics/GraphicSummaryPanel";
import { ResultsPendingPanel } from "@/pages/blastResults/analytics/ResultsPendingPanel";
import { TaxonomyPanel } from "@/pages/blastResults/analytics/TaxonomyPanel";
import { useBlastAnalyticsState } from "@/pages/blastResults/analytics/useBlastAnalyticsState";
import { useBlastResultsState } from "@/pages/blastResults/useBlastResultsState";

/**
 * Single-page BLAST search result view, modelled on NCBI Web BLAST.
 *
 * Tabs (in NCBI order):
 *  Descriptions / Graphic Summary / Alignments / Taxonomy
 * plus two ElasticBLAST-only operator tabs:
 *  Files       — raw output blobs in Azure Storage
 *  Run details — execution timeline + cluster details
 *
 * The active tab is stored in `?tab=...` so deep-links and the browser
 * back/forward buttons keep working. The legacy `/blast/jobs/:id/analytics`
 * route now redirects here with `?tab=descriptions`.
 */
export function BlastResults() {
  const { jobId } = useParams<{ jobId: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const [showCancelConfirm, setShowCancelConfirm] = useState(false);

  const tab = resolveBlastResultsTab(searchParams.get("tab"));
  const state = useBlastResultsState({ jobId, searchParams });
  const { job, isRunning, actions, subscriptionId, storageAccount, resourceGroup } =
    state;
  const hasExplicitTab = searchParams.has("tab");

  useEffect(() => {
    if (!isRunning || hasExplicitTab) return;
    const next = new URLSearchParams(searchParams);
    next.set("tab", "run");
    setSearchParams(next, { replace: true });
  }, [hasExplicitTab, isRunning, searchParams, setSearchParams]);

  const isResultAnalyticsTab =
    tab === "descriptions" ||
    tab === "graphic" ||
    tab === "alignments" ||
    tab === "taxonomy";
  const analyticsEnabled =
    isResultAnalyticsTab && !isRunning && Boolean(job);
  const resultTabWaitingForJob = isResultAnalyticsTab && !job;

  const analytics = useBlastAnalyticsState({
    jobId: jobId ?? "",
    subscriptionId,
    storageAccount,
    resourceGroup,
    enabled: analyticsEnabled,
  });

  return (
    <div className="page-stack">
      <BlastJobHeader
        jobId={jobId!}
        jobTitle={job?.job_title ?? null}
        createdAt={job?.created_at ?? null}
        isRunning={isRunning}
        cancelDisabled={actions.cancelMutation.isPending}
        onRequestCancel={() => setShowCancelConfirm(true)}
        jobPayload={job?.payload}
        program={job?.program ?? null}
        database={job?.db ?? null}
        databaseMetadata={job?.database_metadata ?? null}
        configSnapshot={job?.config_snapshot as Record<string, unknown> | undefined}
        infrastructure={job?.infrastructure as Record<string, unknown> | undefined}
        exportingFormat={actions.exportingFormat}
        onExport={actions.handleExport}
        hasExportTargets={Boolean(subscriptionId && storageAccount)}
      />

      <BlastResultsTabs active={tab} resultsPending={isRunning} />

      {resultTabWaitingForJob && (
        <ResultsPendingPanel
          title="Loading BLAST job"
          message="Checking the run status before loading result data."
        />
      )}
      {tab === "descriptions" && !resultTabWaitingForJob && (
        <DescriptionsTabBody analytics={analytics} resultsPending={isRunning} />
      )}
      {tab === "graphic" && !resultTabWaitingForJob && (
        <GraphicSummaryPanel analytics={analytics} resultsPending={isRunning} />
      )}
      {tab === "alignments" && !resultTabWaitingForJob && (
        <AlignmentsTabBody analytics={analytics} resultsPending={isRunning} />
      )}
      {tab === "taxonomy" && !resultTabWaitingForJob && (
        <TaxonomyPanel
          analytics={analytics}
          jobId={jobId!}
          subscriptionId={subscriptionId}
          storageAccount={storageAccount}
          resourceGroup={resourceGroup}
          resultsPending={isRunning}
        />
      )}
      {tab === "files" && <ResultsCard jobId={jobId!} state={state} />}
      {tab === "run" && (
        <>
          <JobDetailsCard jobId={jobId!} state={state} />
          <ExecutionStepsCard state={state} />
        </>
      )}

      <ConfirmDialog
        open={showCancelConfirm}
        title="Cancel BLAST search"
        message={`Are you sure you want to cancel "${job?.job_title || jobId}"? This will terminate the running orchestrator. Any in-progress work on the AKS cluster may need manual cleanup.`}
        confirmLabel="Cancel search"
        onConfirm={() => {
          setShowCancelConfirm(false);
          actions.cancelMutation.mutate();
        }}
        onCancel={() => setShowCancelConfirm(false)}
      />
    </div>
  );
}
