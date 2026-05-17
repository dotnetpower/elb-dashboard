import { useEffect, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { blastApi } from "@/api/endpoints";
import { FAILURE_PHASES } from "@/components/BlastStepTimeline";
import { loadSavedConfig } from "@/components/SetupWizard";
import { useToast } from "@/components/Toast";
import { useBlastResultActions } from "@/hooks/useBlastResultActions";
import {
  useClusterReadiness,
  useTerminalSidecarHealth,
} from "@/hooks/usePrerequisites";
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

export interface UseBlastResultsStateArgs {
  jobId: string | undefined;
  searchParams: URLSearchParams;
}

/**
 * Owns every query, derivation, side effect and action handler that the
 * Results page needs. The page component just reads state out and renders
 * sections.
 */
export function useBlastResultsState({
  jobId,
  searchParams,
}: UseBlastResultsStateArgs) {
  const config = loadSavedConfig();
  const { toast } = useToast();
  const queryClient = useQueryClient();
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
    queryKey: [
      "blast-results",
      jobId,
      subscriptionId,
      storageAccount,
      resourceGroup,
    ],
    queryFn: () =>
      blastApi.listResults(
        jobId!,
        subscriptionId,
        storageAccount,
        resourceGroup,
      ),
    enabled: Boolean(jobId && subscriptionId && storageAccount),
    refetchInterval: (q) => {
      if (q.state.data?.files && q.state.data.files.length > 0) return false;
      if (q.state.data?.public_access_disabled) return false;
      return 30_000;
    },
  });

  const job = jobQuery.data;
  const allFiles = resultsQuery.data?.files ?? [];
  const split = splitBlastResultFiles(allFiles);
  const publicAccessDisabled =
    resultsQuery.data?.public_access_disabled === true;

  const phaseInfo = resolveBlastJobPhase(job);
  const resultState = resolveBlastResultState({
    job,
    phase: phaseInfo.phase,
    customStatus: phaseInfo.customStatus,
    output: phaseInfo.output,
    outputStatus: phaseInfo.outputStatus,
    isJobFailed: phaseInfo.isJobFailed,
  });

  const actions = useBlastResultActions({
    jobId,
    subscriptionId,
    storageAccount,
  });

  // Phase transition toaster — only fire when we observe a LIVE transition
  // (running → terminal). Skip toasts when the job was already terminal on
  // first load. ``prevPhaseRef`` and ``initialPhaseRef`` are read+written
  // inside the effect; ``phase`` is the only meaningful trigger.
  const prevPhaseRef = useRef<string | null>(null);
  const initialPhaseRef = useRef<string | null>(null);
  useEffect(() => {
    if (!job) return;
    const phase = phaseInfo.phase;
    if (prevPhaseRef.current === null) {
      prevPhaseRef.current = phase;
      initialPhaseRef.current = phase;
      return;
    }
    if (
      initialPhaseRef.current &&
      TERMINAL_PHASES.has(initialPhaseRef.current)
    ) {
      prevPhaseRef.current = phase;
      return;
    }
    if (phase && phase !== prevPhaseRef.current) {
      if (phase === "completed")
        toast("BLAST job completed successfully!", "success");
      else if (FAILURE_PHASES.has(phase)) toast("BLAST job failed.", "error");
    }
    prevPhaseRef.current = phase;
  }, [job, phaseInfo.phase, toast]);

  const hasExportTargets = Boolean(subscriptionId && storageAccount);
  const showCompletedMetrics =
    Boolean(job) &&
    phaseInfo.phase === "completed" &&
    !resultState.effectiveIsFailed &&
    split.files.length > 0;

  return {
    // identity
    subscriptionId,
    storageAccount,
    resourceGroup,
    // queries
    jobQuery,
    resultsQuery,
    job,
    // derived
    ...split,
    publicAccessDisabled,
    ...phaseInfo,
    ...resultState,
    // prerequisites / actions
    cluster,
    terminalSidecar,
    actions,
    queryClient,
    // computed flags
    hasExportTargets,
    showCompletedMetrics,
  } as const;
}

export type BlastResultsState = ReturnType<typeof useBlastResultsState>;
