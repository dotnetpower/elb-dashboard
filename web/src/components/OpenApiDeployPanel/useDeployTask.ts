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
  storageAccount: string;
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
  storageAccount,
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

  const waitElapsed = Math.max(0, Math.floor((now - deployStartedAt) / 1000));
  const deployOutput = deployStatusQuery.data?.output;
  const deployCustomStatus = deployStatusQuery.data?.custom_status as
    | { phase?: string }
    | null
    | undefined;
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

  const handleCancelTracking = () => {
    setDeployInstanceId(null);
    setDeployError(null);
    clearStoredDeploy(storageKey);
  };

  const canDeploy =
    Boolean(subscriptionId && resourceGroup && clusterName && acrName) &&
    imageBuilt &&
    !deployIsActive;

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
  } as const;
}
