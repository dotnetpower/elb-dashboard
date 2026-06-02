import { useEffect, useRef, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { AlertTriangle, RefreshCw } from "lucide-react";

import { ConfirmDialog } from "@/components/ConfirmDialog";
import { BlastJobHeader } from "@/pages/blastResults/BlastJobHeader";
import {
  BlastResultsTabs,
  resolveBlastResultsTab,
  shouldOpenRunDetailsForFailedJob,
} from "@/pages/blastResults/BlastResultsTabs";
import { ExecutionStepsCard } from "@/pages/blastResults/ExecutionStepsCard";
import { JobDetailsCard } from "@/pages/blastResults/JobDetailsCard";
import { ResultsCard } from "@/pages/blastResults/ResultsCard";
import { AlignmentsTabBody } from "@/pages/blastResults/analytics/AlignmentsTabBody";
import { DescriptionsTabBody } from "@/pages/blastResults/analytics/DescriptionsTabBody";
import { GraphicSummaryPanel } from "@/pages/blastResults/analytics/GraphicSummaryPanel";
import { ResultPassportCard } from "@/pages/blastResults/analytics/ResultPassportCard";
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
  const effectivePhase = state.effectivePhase;
  const hasExplicitTab = searchParams.has("tab");
  const justSubmitted = searchParams.get("submitted") === "1";
  const canRequestCancel =
    isRunning || Boolean(justSubmitted && jobId && !job && state.jobQuery.isFetching);
  const failedTabRedirectRef = useRef<string | null>(null);

  useEffect(() => {
    if (!canRequestCancel || hasExplicitTab) return;
    const next = new URLSearchParams(searchParams);
    next.set("tab", "run");
    setSearchParams(next, { replace: true });
  }, [canRequestCancel, hasExplicitTab, searchParams, setSearchParams]);

  useEffect(() => {
    if (!jobId || failedTabRedirectRef.current === jobId) return;
    if (!state.effectiveIsFailed) return;
    failedTabRedirectRef.current = jobId;
    if (!shouldOpenRunDetailsForFailedJob(tab, state.effectiveIsFailed)) return;
    const next = new URLSearchParams(searchParams);
    next.set("tab", "run");
    setSearchParams(next, { replace: true });
  }, [jobId, tab, state.effectiveIsFailed, searchParams, setSearchParams]);

  // When the job transitions to `completed` while the user is still on the
  // execution-timeline tab, auto-switch to the Descriptions analytics tab.
  // We only fire this on the rising edge of the phase (previous value was
  // non-completed) so a user who navigates BACK to "run" on an already
  // completed job is not flipped away again. The transition is captured
  // here in the URL so deep-link / back-button stays predictable.
  const previousPhaseRef = useRef<string | null>(null);
  useEffect(() => {
    const previous = previousPhaseRef.current;
    previousPhaseRef.current = effectivePhase;
    if (!effectivePhase || !jobId) return;
    if (previous === effectivePhase) return;
    if (effectivePhase !== "completed") return;
    if (tab !== "run") return;
    const next = new URLSearchParams(searchParams);
    next.set("tab", "descriptions");
    setSearchParams(next, { replace: true });
  }, [effectivePhase, jobId, tab, searchParams, setSearchParams]);

  const isResultAnalyticsTab =
    tab === "descriptions" ||
    tab === "graphic" ||
    tab === "alignments" ||
    tab === "taxonomy";
  const analyticsEnabled = isResultAnalyticsTab && !isRunning && Boolean(job);
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
        updatedAt={job?.updated_at ?? null}
        isRunning={isRunning}
        canCancel={canRequestCancel}
        cancelDisabled={actions.cancelMutation.isPending}
        onRequestCancel={() => setShowCancelConfirm(true)}
        jobPayload={job?.payload}
        program={job?.program ?? null}
        database={job?.db ?? null}
        customStatus={job?.custom_status}
        databaseMetadata={state.databaseMetadata}
        configSnapshot={job?.config_snapshot as Record<string, unknown> | undefined}
        infrastructure={job?.infrastructure as Record<string, unknown> | undefined}
        exportingFormat={actions.exportingFormat}
        onExport={actions.handleExport}
        hasExportTargets={state.showCompletedMetrics}
        resultFiles={state.resultFiles}
      />

      {state.liveUpdatesStalled && (
        <div
          role="status"
          aria-live="polite"
          style={{
            display: "flex",
            alignItems: "flex-start",
            gap: 10,
            padding: "10px 14px",
            background: "rgba(242,180,111,0.08)",
            border: "1px solid rgba(242,180,111,0.24)",
            borderRadius: 8,
            fontSize: 12,
            color: "var(--warning)",
          }}
        >
          <AlertTriangle size={14} style={{ flexShrink: 0, marginTop: 2 }} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontWeight: 600 }}>Live status updates paused</div>
            <div style={{ color: "var(--text-muted)", marginTop: 2 }}>
              {state.liveUpdatesStalledAuthExpired
                ? "Your browser sign-in session may have expired, so the status below could be out of date — this job may have already finished. Sign in again or refresh to load the latest result."
                : "The dashboard could not refresh this job's status, so the status below could be out of date. Refresh to load the latest result."}
            </div>
          </div>
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={() => void state.jobQuery.refetch()}
            disabled={state.jobQuery.isFetching}
            style={{ flexShrink: 0 }}
          >
            <RefreshCw
              size={13}
              className={state.jobQuery.isFetching ? "spin" : undefined}
            />
            Refresh now
          </button>
        </div>
      )}

      <BlastResultsTabs active={tab} resultsPending={isRunning} />

      {isResultAnalyticsTab && !isRunning && job && (
        <ResultPassportCard job={job} />
      )}

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
