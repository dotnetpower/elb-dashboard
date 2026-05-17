import { useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";

import { ConfirmDialog } from "@/components/ConfirmDialog";
import { BlastJobHeader } from "@/pages/blastResults/BlastJobHeader";
import { ExecutionStepsCard } from "@/pages/blastResults/ExecutionStepsCard";
import { JobDetailsCard } from "@/pages/blastResults/JobDetailsCard";
import { ResultsCard } from "@/pages/blastResults/ResultsCard";
import { useBlastResultsState } from "@/pages/blastResults/useBlastResultsState";

export function BlastResults() {
  const { jobId } = useParams<{ jobId: string }>();
  const [searchParams] = useSearchParams();
  const [showCancelConfirm, setShowCancelConfirm] = useState(false);

  const state = useBlastResultsState({ jobId, searchParams });
  const { job, isRunning, actions } = state;

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
      />

      <JobDetailsCard jobId={jobId!} state={state} />

      <ExecutionStepsCard state={state} />

      <ResultsCard jobId={jobId!} state={state} />

      <ConfirmDialog
        open={showCancelConfirm}
        title="Cancel BLAST Job"
        message={`Are you sure you want to cancel "${job?.job_title || jobId}"? This will terminate the running orchestrator. Any in-progress work on the AKS cluster may need manual cleanup.`}
        confirmLabel="Cancel Job"
        onConfirm={() => {
          setShowCancelConfirm(false);
          actions.cancelMutation.mutate();
        }}
        onCancel={() => setShowCancelConfirm(false)}
      />
    </div>
  );
}
