import { useEffect, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import type { ApiError } from "@/api/client";
import { blastApi } from "@/api/endpoints";
import { FAILURE_PHASES } from "@/components/BlastStepTimeline";
import { loadSavedConfig } from "@/components/SetupWizard";
import { useToast } from "@/components/Toast";
import { useBlastResultActions } from "@/hooks/useBlastResultActions";
import { useClusterReadiness, useTerminalSidecarHealth } from "@/hooks/usePrerequisites";
import { resolveBlastJobScope } from "@/pages/blastResults/blastJobScope";
import {
  resolveBlastJobPhase,
  resolveBlastResultState,
  splitBlastResultFiles,
} from "@/pages/blastResultsModel";
import { isFeatureEnabled } from "@/config/runtime";

const TERMINAL_PHASES = new Set([
  "completed",
  "failed",
  "error",
  "database_unavailable",
  "submit_failed",
  "warmup_failed",
  "cancelled",
]);

// Sub-second visual feedback matters for the early pipeline because some
// phases (`staging_db`) finish in 0 ms and others (`configuring`) in 2–3 s.
// A 5 s poll cadence used to hide both transitions behind a single tick, so
// users perceived a stall right after Warmup Check. Poll these fast-moving
// phases at 1 s; once the workload is doing real work (BLAST run / export)
// the cadence relaxes to 3 s to keep ARM/Storage load reasonable.
const FAST_POLL_PHASES = new Set([
  "preparing",
  "warming_up",
  "configuring",
  "staging_db",
  "submitting",
  "waiting_for_submit_slot",
]);

const FAST_POLL_INTERVAL_MS = 1_000;
const STEADY_POLL_INTERVAL_MS = 3_000;
// After a job reaches a terminal phase the worker / reconcile beat can still
// backfill trailing artefacts (K8s pod log tails on `running.last_output`,
// final export logs, …) for ~30 s. If we stop polling the moment phase flips
// to `completed`/`failed`, the dashboard renders the partial snapshot
// indefinitely (TanStack Query has no reason to refetch). Keep a slow
// terminal poll going so trailing artefacts surface on the next tick.
const TERMINAL_BACKFILL_POLL_INTERVAL_MS = 10_000;
const TERMINAL_BACKFILL_WINDOW_MS = 5 * 60_000;

const RESULTS_READY_PHASES = new Set([
  "completed",
  "results_pending",
  "failed",
  "error",
  "database_unavailable",
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
export function useBlastResultsState({ jobId, searchParams }: UseBlastResultsStateArgs) {
  const config = loadSavedConfig();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const cluster = useClusterReadiness();
  const terminalEnabled = isFeatureEnabled("terminal");
  const terminalSidecar = useTerminalSidecarHealth(terminalEnabled);

  const jobQuery = useQuery({
    queryKey: ["blast-job", jobId],
    queryFn: () => blastApi.getJob(jobId!, { includeDatabaseMetadata: false }),
    enabled: Boolean(jobId),
    // After the TERMINAL_BACKFILL_WINDOW_MS polling window ends, the page
    // sits on whatever snapshot was current at the moment polling stopped.
    // If the user comes back to the tab later (or remounts the page by
    // navigating away and back), we want them to see the latest backfilled
    // pod logs / export logs instead of the stale tail-end snapshot.
    refetchOnWindowFocus: true,
    refetchOnMount: "always",
    refetchInterval: (q) => {
      const d = q.state.data;
      if (!d) return FAST_POLL_INTERVAL_MS;
      const resolved = resolveBlastJobPhase(d);
      if (TERMINAL_PHASES.has(resolved.phase) || FAILURE_PHASES.has(resolved.phase)) {
        // Keep polling slowly for a bounded window after the job reaches a
        // terminal phase so the reconcile beat's trailing artefact writes
        // (e.g., K8s pod log tails captured into `running.last_output`)
        // surface in the UI. Stop entirely once the row has been stable
        // long enough that no further backfill is expected.
        const updatedAt = (d as { updated_at?: string }).updated_at;
        if (typeof updatedAt === "string" && updatedAt) {
          const ageMs = Date.now() - new Date(updatedAt).getTime();
          if (Number.isFinite(ageMs) && ageMs > TERMINAL_BACKFILL_WINDOW_MS) {
            return false;
          }
        }
        return TERMINAL_BACKFILL_POLL_INTERVAL_MS;
      }
      return FAST_POLL_PHASES.has(resolved.phase)
        ? FAST_POLL_INTERVAL_MS
        : STEADY_POLL_INTERVAL_MS;
    },
  });

  // BlastJobHeader renders "DB title / DB sequences / DB letters / DB snapshot".
  // The polling jobQuery above intentionally omits database_metadata to keep the
  // 3-5s refetch cheap (resolving it hits Azure Storage for the .njs/.metadata
  // blobs). Fetch it once with a long staleTime so the header rows stay
  // populated for the lifetime of the page without paying that cost on every
  // poll tick.
  const databaseMetadataQuery = useQuery({
    queryKey: ["blast-job-metadata", jobId],
    queryFn: () => blastApi.getJob(jobId!, { includeDatabaseMetadata: true }),
    enabled: Boolean(jobId),
    staleTime: Number.POSITIVE_INFINITY,
    gcTime: 60 * 60 * 1000,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    refetchInterval: false,
  });

  const job = jobQuery.data;
  const databaseMetadata =
    databaseMetadataQuery.data?.database_metadata ?? job?.database_metadata ?? null;
  const payload = job?.payload;

  // The backend records the cluster this job actually ran on in
  // `job.infrastructure`. The cluster picker is subscription-wide, so a job's
  // cluster may live OUTSIDE the workspace anchor RG. When the submit payload
  // is missing a field (legacy jobs, or fields the backend never echoes back),
  // we must fall back to the job's own infrastructure block — NOT the workspace
  // anchor RG/cluster — otherwise results listing, downloads, exports, and
  // cancel all target the wrong resource group/cluster for cross-RG fleets.
  const { subscriptionId, storageAccount, resourceGroup, clusterName } = resolveBlastJobScope({
    searchParams,
    payload,
    infrastructure: job?.infrastructure,
    config,
  });

  const phaseInfo = resolveBlastJobPhase(job);

  const resultsQuery = useQuery({
    queryKey: ["blast-results", jobId, subscriptionId, storageAccount, resourceGroup],
    queryFn: () =>
      blastApi.listResults(jobId!, subscriptionId, storageAccount, resourceGroup),
    enabled: Boolean(
      jobId && subscriptionId && storageAccount && RESULTS_READY_PHASES.has(phaseInfo.phase),
    ),
    refetchInterval: (q) => {
      if (q.state.data?.files && q.state.data.files.length > 0) return false;
      if (q.state.data?.public_access_disabled) return false;
      return 30_000;
    },
  });

  const allFiles = resultsQuery.data?.files ?? [];
  const split = splitBlastResultFiles(allFiles);
  const publicAccessDisabled = resultsQuery.data?.public_access_disabled === true;
  const resultState = resolveBlastResultState({
    job,
    phase: phaseInfo.phase,
    customStatus: phaseInfo.customStatus,
    output: phaseInfo.output,
    outputStatus: phaseInfo.outputStatus,
    isJobFailed: phaseInfo.isJobFailed,
  });

  const executionStepsQuery = useQuery({
    queryKey: ["blast-execution-steps", jobId],
    queryFn: () => blastApi.getExecutionSteps(jobId!),
    enabled: Boolean(jobId && job && TERMINAL_PHASES.has(phaseInfo.phase)),
    staleTime: 10_000,
    // Same rationale as jobQuery: refetch on tab focus / remount so a user
    // who returns to the page hours later gets the latest live state
    // instead of whatever the snapshot looked like when polling stopped.
    refetchOnWindowFocus: true,
    refetchOnMount: "always",
    refetchInterval: (q) => {
      const data = q.state.data;
      const artifactState = data?.artifact_state;
      // While the artifact bundle is still being assembled we poll fast.
      if (artifactState && artifactState !== "ready" && artifactState !== "inline_fallback" && artifactState !== "missing") {
        return 5_000;
      }
      // After the artifact bundle is "ready" we KEEP polling at a slow
      // cadence for the same TERMINAL_BACKFILL_WINDOW_MS used by the main
      // job query. Reconcile beat appends K8s pod log tails into
      // `running.last_output` AFTER the job is in `phase=completed`; if we
      // stopped polling the moment artifact_state flipped to ready, the
      // dashboard would render an execution-steps snapshot that is missing
      // those tails forever (until the user manually refreshed).
      const updatedAt = (data as { updated_at?: string } | undefined)?.updated_at;
      if (typeof updatedAt === "string" && updatedAt) {
        const ageMs = Date.now() - new Date(updatedAt).getTime();
        if (Number.isFinite(ageMs) && ageMs > TERMINAL_BACKFILL_WINDOW_MS) {
          return false;
        }
      }
      return TERMINAL_BACKFILL_POLL_INTERVAL_MS;
    },
    retry: 3,
  });

  const executionStepsJob =
    job && executionStepsQuery.data
      ? {
          ...job,
          custom_status: executionStepsQuery.data.custom_status ?? job.custom_status,
          output: executionStepsQuery.data.output ?? job.output,
        }
      : job;

  const actions = useBlastResultActions({
    jobId,
    subscriptionId,
    resourceGroup,
    clusterName,
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
    if (initialPhaseRef.current && TERMINAL_PHASES.has(initialPhaseRef.current)) {
      prevPhaseRef.current = phase;
      return;
    }
    if (phase && phase !== prevPhaseRef.current) {
      if (phase === "completed") toast("BLAST job completed successfully!", "success");
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

  // When the job-status poll starts failing while the displayed phase is still
  // non-terminal, TanStack Query keeps the last successful snapshot mounted, so
  // the page can sit on a stale "running" badge for a job that has actually
  // finished. The most common cause is an expired MSAL session: the poll 401s,
  // the global gate routes to sign-in, but a backgrounded tab may never follow
  // the redirect and is left staring at the stale snapshot. Expose explicit
  // flags so the page can render a "live updates paused" banner with a manual
  // refresh action instead of silently misleading the user.
  const jobQueryErrorStatus = (jobQuery.error as ApiError | null)?.status;
  const liveUpdatesStalled =
    jobQuery.isError && Boolean(job) && !TERMINAL_PHASES.has(phaseInfo.phase);
  const liveUpdatesStalledAuthExpired =
    liveUpdatesStalled && jobQueryErrorStatus === 401;

  return {
    // identity
    subscriptionId,
    storageAccount,
    resourceGroup,
    clusterName,
    // queries
    jobQuery,
    databaseMetadataQuery,
    resultsQuery,
    executionStepsQuery,
    job,
    databaseMetadata,
    executionStepsJob,
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
    liveUpdatesStalled,
    liveUpdatesStalledAuthExpired,
  } as const;
}

export type BlastResultsState = ReturnType<typeof useBlastResultsState>;
