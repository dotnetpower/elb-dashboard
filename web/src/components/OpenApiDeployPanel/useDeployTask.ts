import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { formatApiError } from "@/api/client";
import { aksApi } from "@/api/endpoints";

import {
  clearStoredDeploy,
  DEPLOY_DISCOVERY_TIMEOUT_MS,
  FINISHED_STATUSES,
  deployStorageKey,
  readStoredDeploy,
  STALE_PENDING_TIMEOUT_MS,
  writeStoredDeploy,
} from "./storageHelpers";

export type DeployState = "idle" | "deploying" | "waiting" | "error";

export interface UseDeployTaskArgs {
  subscriptionId: string;
  resourceGroup: string;
  clusterName: string;
  acrName: string;
  acrResourceGroup: string;
  storageAccount: string;
  storageResourceGroup: string;
  imageBuilt: boolean;
  onRetry: () => void;
}

/**
 * Owns the full lifecycle of a single OpenAPI deploy task: localStorage
 * persistence so a refresh resumes tracking, the Celery status query,
 * and every guard effect (success-discovery, failure detection, stale
 * pending, beforeunload prompt). The component is a pure renderer.
 */
export function useDeployTask({
  subscriptionId,
  resourceGroup,
  clusterName,
  acrName,
  acrResourceGroup,
  storageAccount,
  storageResourceGroup,
  imageBuilt,
  onRetry,
}: UseDeployTaskArgs) {
  const storageKey = useMemo(
    () => deployStorageKey(subscriptionId, resourceGroup, clusterName),
    [subscriptionId, resourceGroup, clusterName],
  );
  const [deployInstanceId, setDeployInstanceId] = useState<string | null>(
    () => readStoredDeploy(storageKey)?.instanceId ?? null,
  );
  const [deployStartedAt, setDeployStartedAt] = useState<number>(
    () => readStoredDeploy(storageKey)?.startedAt ?? Date.now(),
  );
  const [startingDeploy, setStartingDeploy] = useState(false);
  const [deployError, setDeployError] = useState<string | null>(null);
  const [now, setNow] = useState(Date.now());
  // Rebuild-and-redeploy: a separate orchestrator task id we poll while the
  // ACR image builds. Once the build succeeds the status carries a
  // ``deploy_task_id`` and we hand off to the normal deploy tracking above.
  const [rebuildInstanceId, setRebuildInstanceId] = useState<string | null>(null);
  const [startingRebuild, setStartingRebuild] = useState(false);

  useEffect(() => {
    const stored = readStoredDeploy(storageKey);
    setDeployInstanceId(stored?.instanceId ?? null);
    setDeployStartedAt(stored?.startedAt ?? Date.now());
    setDeployError(null);
  }, [storageKey]);

  useEffect(() => {
    if (!deployInstanceId) return undefined;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [deployInstanceId]);

  const deployStatusQuery = useQuery({
    queryKey: ["openapi-deploy-status", deployInstanceId],
    queryFn: () => aksApi.openApiDeployStatus(deployInstanceId!),
    enabled: Boolean(deployInstanceId),
    refetchInterval: (query) => {
      const status = query.state.data?.runtime_status;
      return status && FINISHED_STATUSES.has(status) ? false : 5_000;
    },
    retry: 1,
  });

  const rebuildStatusQuery = useQuery({
    queryKey: ["openapi-rebuild-status", rebuildInstanceId],
    queryFn: () => aksApi.rebuildDeployOpenApiStatus(rebuildInstanceId!),
    enabled: Boolean(rebuildInstanceId),
    refetchInterval: (query) => {
      const status = query.state.data?.runtime_status;
      return status && FINISHED_STATUSES.has(status) ? false : 5_000;
    },
    retry: 1,
  });
  const rebuildOutput = rebuildStatusQuery.data?.output;
  const rebuildPhase =
    (rebuildStatusQuery.data?.custom_status as { phase?: string } | null | undefined)
      ?.phase ?? null;
  const rebuildInProgress = startingRebuild || Boolean(rebuildInstanceId);

  // Build succeeded → hand off to the normal deploy tracking by adopting the
  // chained deploy task id (so the existing status banner + guards take over).
  useEffect(() => {
    if (!rebuildInstanceId) return;
    const deployTaskId = rebuildOutput?.deploy_task_id;
    if (
      rebuildStatusQuery.data?.runtime_status === "Completed" &&
      rebuildOutput?.status !== "failed" &&
      deployTaskId
    ) {
      const startedAt = Date.now();
      setDeployStartedAt(startedAt);
      setDeployInstanceId(deployTaskId);
      writeStoredDeploy(storageKey, deployTaskId, startedAt);
      setRebuildInstanceId(null);
    }
  }, [
    rebuildInstanceId,
    rebuildOutput?.deploy_task_id,
    rebuildOutput?.status,
    rebuildStatusQuery.data?.runtime_status,
    storageKey,
  ]);

  // Build failed (or the orchestrator task failed) → surface the error and
  // stop rebuild tracking; deploy is never reached when the build fails.
  useEffect(() => {
    if (!rebuildInstanceId) return;
    const rs = rebuildStatusQuery.data?.runtime_status;
    const failed =
      rs === "Failed" ||
      rs === "Terminated" ||
      (rs === "Completed" && rebuildOutput?.status === "failed");
    if (failed) {
      const code = rebuildOutput?.error_code;
      setDeployError(`OpenAPI rebuild failed${code ? ` (${code})` : ""}.`);
      setRebuildInstanceId(null);
    }
  }, [
    rebuildInstanceId,
    rebuildOutput?.status,
    rebuildOutput?.error_code,
    rebuildStatusQuery.data?.runtime_status,
  ]);

  const waitElapsed = Math.max(0, Math.floor((now - deployStartedAt) / 1000));
  const deployOutput = deployStatusQuery.data?.output;
  const deployCustomStatus = deployStatusQuery.data?.custom_status as
    | { phase?: string }
    | null
    | undefined;
  // Surface the additive envelope-root recovery affordance the backend
  // injects on upstream-reach (VNet peering) failures. The SPA passes
  // this down to DeployStatusBanner which renders <RepairPeeringButton>.
  // Falls back to the thrown ApiError body when the status query itself
  // errors out (e.g. the route returned 502 with the same payload).
  const deployRecoveryAction =
    (deployStatusQuery.data?.recovery_action as string | undefined) ?? null;
  const deployRecoveryHint =
    (deployStatusQuery.data?.recovery_hint as string | undefined) ?? null;
  const deploySucceeded =
    deployStatusQuery.data?.runtime_status === "Completed" &&
    deployOutput?.status === "succeeded";
  const deployFailed =
    deployStatusQuery.data?.runtime_status === "Failed" ||
    deployStatusQuery.data?.runtime_status === "Terminated" ||
    (deployStatusQuery.data?.runtime_status === "Completed" &&
      deployOutput?.status === "failed");
  const deployInProgress = Boolean(
    deployInstanceId &&
      !deploySucceeded &&
      !deployFailed &&
      !deployStatusQuery.isError,
  );
  const deployIsActive =
    startingDeploy || deployInProgress || deploySucceeded;
  const deployState: DeployState = startingDeploy
    ? "deploying"
    : deployFailed || deployStatusQuery.isError || deployError
      ? "error"
      : deploySucceeded
        ? "waiting"
        : deployInProgress
          ? "deploying"
          : "idle";

  // While a deploy is mid-flight, ask the browser to confirm before
  // refresh / tab-close. Losing the page mid-deploy doesn't kill the
  // worker (the task continues server-side and the instance id is
  // persisted to localStorage), but the user can no longer watch
  // progress and frequently re-clicks Deploy thinking it failed.
  useEffect(() => {
    if (!startingDeploy && !deployInProgress) return undefined;
    const handler = (event: BeforeUnloadEvent) => {
      // Modern browsers ignore the custom string, but `preventDefault`
      // + a non-empty `returnValue` is the documented way to trigger
      // the native confirmation dialog.
      event.preventDefault();
      event.returnValue =
        "OpenAPI deploy is still running. Leaving now will stop progress updates (the deploy itself continues in the background).";
      return event.returnValue;
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [startingDeploy, deployInProgress]);

  useEffect(() => {
    if (!deployInstanceId || !deploySucceeded) return undefined;
    onRetry();
    const interval = window.setInterval(() => {
      if (Date.now() - deployStartedAt <= DEPLOY_DISCOVERY_TIMEOUT_MS) {
        onRetry();
      }
    }, 5_000);
    return () => window.clearInterval(interval);
  }, [deployInstanceId, deployStartedAt, deploySucceeded, onRetry]);

  useEffect(() => {
    if (!deployFailed) return;
    const message =
      deployOutput?.openapi_deploy?.error ??
      deployOutput?.workload_identity?.error ??
      (deployStatusQuery.data?.runtime_status === "Terminated"
        ? "OpenAPI deploy was terminated."
        : "OpenAPI deploy failed.");
    setDeployError(message);
    clearStoredDeploy(storageKey);
  }, [
    deployFailed,
    deployOutput,
    deployStatusQuery.data?.runtime_status,
    storageKey,
  ]);

  useEffect(() => {
    if (!deployInstanceId || !deploySucceeded) return;
    if (now - deployStartedAt <= DEPLOY_DISCOVERY_TIMEOUT_MS) return;
    setDeployInstanceId(null);
    clearStoredDeploy(storageKey);
  }, [deployInstanceId, deployStartedAt, deploySucceeded, now, storageKey]);

  useEffect(() => {
    if (!deployStatusQuery.isError || !deployInstanceId) return;
    setDeployError(
      "Previous OpenAPI deploy status could not be restored. Start deploy again.",
    );
    setDeployInstanceId(null);
    clearStoredDeploy(storageKey);
  }, [deployInstanceId, deployStatusQuery.isError, storageKey]);

  // Stale-PENDING guard: Celery's AsyncResult returns "PENDING" both
  // for "task hasn't started yet" and "task id is unknown to the
  // backend". If we sit on Pending for STALE_PENDING_TIMEOUT_MS without
  // ever transitioning to Running, treat the task as lost so the user
  // can deploy again.
  useEffect(() => {
    if (!deployInstanceId) return;
    if (deployStatusQuery.data?.runtime_status !== "Pending") return;
    if (now - deployStartedAt < STALE_PENDING_TIMEOUT_MS) return;
    setDeployError(
      "OpenAPI deploy never started (the worker may not be running). Click Deploy to retry.",
    );
    setDeployInstanceId(null);
    clearStoredDeploy(storageKey);
  }, [
    deployInstanceId,
    deployStartedAt,
    deployStatusQuery.data?.runtime_status,
    now,
    storageKey,
  ]);

  const handleCancelTracking = async () => {
    const taskId = deployInstanceId;
    // Always clear local tracking first so the UI is responsive even if
    // the revoke call hangs / fails. The backend route is idempotent so
    // a duplicate cancel from a re-clicked button is a no-op.
    setDeployInstanceId(null);
    setDeployError(null);
    clearStoredDeploy(storageKey);
    if (!taskId) return;
    try {
      await aksApi.cancelOpenApiDeploy(taskId);
    } catch (err: unknown) {
      // Surface the failure but keep the UI unlocked — the worker may
      // still be running, but the user explicitly chose to stop
      // tracking. They can hit Deploy again to retry; the new task id
      // will replace this one in localStorage.
      setDeployError(
        `Deploy cancel sent to server but the response was not OK: ${formatApiError(err)}. The Celery task may still be running; refresh to re-discover.`,
      );
    }
  };

  const canDeploy =
    Boolean(subscriptionId && resourceGroup && clusterName && acrName) &&
    imageBuilt &&
    !deployIsActive &&
    !rebuildInProgress;

  // Rebuild rebuilds the image first, so it does NOT require imageBuilt.
  const canRebuild =
    Boolean(subscriptionId && resourceGroup && clusterName && acrName) &&
    !deployIsActive &&
    !rebuildInProgress;

  const handleDeploy = async () => {
    setStartingDeploy(true);
    setDeployError(null);
    setDeployInstanceId(null);
    clearStoredDeploy(storageKey);
    try {
      const response = await aksApi.deployOpenApi(
        subscriptionId,
        resourceGroup,
        clusterName,
        acrName,
        storageAccount,
        storageResourceGroup,
        acrResourceGroup,
      );
      const startedAt = Date.now();
      setDeployStartedAt(startedAt);
      setDeployInstanceId(response.id);
      writeStoredDeploy(storageKey, response.id, startedAt);
    } catch (err: unknown) {
      setDeployError(formatApiError(err));
    } finally {
      setStartingDeploy(false);
    }
  };

  const handleRebuildDeploy = async () => {
    setStartingRebuild(true);
    setDeployError(null);
    setDeployInstanceId(null);
    clearStoredDeploy(storageKey);
    try {
      const response = await aksApi.rebuildDeployOpenApi(
        subscriptionId,
        resourceGroup,
        clusterName,
        acrName,
        storageAccount,
        storageResourceGroup,
        acrResourceGroup,
      );
      setRebuildInstanceId(response.id);
    } catch (err: unknown) {
      setDeployError(formatApiError(err));
    } finally {
      setStartingRebuild(false);
    }
  };

  return {
    deployInstanceId,
    deployState,
    deployError,
    deployCustomStatus,
    deploySucceeded,
    waitElapsed,
    canDeploy,
    handleDeploy,
    handleCancelTracking,
    deployRecoveryAction,
    deployRecoveryHint,
    canRebuild,
    handleRebuildDeploy,
    rebuildInProgress,
    rebuildPhase,
  } as const;
}
