import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Loader2,
  AlertTriangle,
  RefreshCw,
  Package,
  Rocket,
  X,
  RotateCw,
} from "lucide-react";

import { aksApi } from "@/api/endpoints";
import { formatApiError } from "@/api/client";

interface Props {
  subscriptionId: string;
  resourceGroup: string;
  clusterName: string;
  acrName: string;
  storageAccount: string;
  imageBuilt: boolean;
  onRetry: () => void;
  retrying: boolean;
  /**
   * `deploy` (default) — service is not running, render the warning panel
   * with the **Deploy elb-openapi** button.
   * `update` — service IS running, render a compact panel with an
   * **Update** button so the user can re-roll the deployment after the
   * pinned image tag changes upstream.
   */
  variant?: "deploy" | "update";
  /** Tag pinned in this dashboard (`api/services/image_tags.py`). */
  pinnedTag?: string;
  /** Tag currently present in ACR. */
  currentTag?: string;
}

const FINISHED_STATUSES = new Set(["Completed", "Failed", "Terminated"]);
const DEPLOY_DISCOVERY_TIMEOUT_MS = 180_000;
// If the deploy task stays in "Pending" (Celery has no record of it ever
// starting) for this long, assume the task was lost — typically because the
// api enqueued it to a broker the worker is not consuming from, or the task
// id was wiped from the result backend. Surface a clear error instead of
// trapping the panel in a permanent "Deploying..." state.
const STALE_PENDING_TIMEOUT_MS = 300_000;

function deployStorageKey(subscriptionId: string, resourceGroup: string, clusterName: string) {
  return `elb-openapi-deploy-${subscriptionId}-${resourceGroup}-${clusterName}`;
}

function readStoredDeploy(key: string): { instanceId: string; startedAt: number } | null {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { instanceId?: string; startedAt?: number };
    if (!parsed.instanceId || !parsed.startedAt) return null;
    return { instanceId: parsed.instanceId, startedAt: parsed.startedAt };
  } catch {
    return null;
  }
}

function writeStoredDeploy(key: string, instanceId: string, startedAt: number) {
  try {
    localStorage.setItem(key, JSON.stringify({ instanceId, startedAt }));
  } catch {
    /* best-effort */
  }
}

function clearStoredDeploy(key: string) {
  try {
    localStorage.removeItem(key);
  } catch {
    /* best-effort */
  }
}

function formatDeployPhase(phase?: string) {
  switch (phase) {
    case "setup_workload_identity":
      return "Setting up workload identity";
    case "deploying_openapi":
      return "Deploying OpenAPI service";
    default:
      return phase ?? "Deploying OpenAPI";
  }
}

export function OpenApiDeployPanel({
  subscriptionId,
  resourceGroup,
  clusterName,
  acrName,
  storageAccount,
  imageBuilt,
  onRetry,
  retrying,
  variant = "deploy",
  pinnedTag,
  currentTag,
}: Props) {
  const storageKey = useMemo(
    () => deployStorageKey(subscriptionId, resourceGroup, clusterName),
    [subscriptionId, resourceGroup, clusterName],
  );
  const [deployInstanceId, setDeployInstanceId] = useState<string | null>(() => {
    return readStoredDeploy(storageKey)?.instanceId ?? null;
  });
  const [deployStartedAt, setDeployStartedAt] = useState<number>(() => {
    return readStoredDeploy(storageKey)?.startedAt ?? Date.now();
  });
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
    deployStatusQuery.data?.runtime_status === "Completed" && deployOutput?.status === "succeeded";
  const deployFailed =
    deployStatusQuery.data?.runtime_status === "Failed" ||
    deployStatusQuery.data?.runtime_status === "Terminated" ||
    (deployStatusQuery.data?.runtime_status === "Completed" && deployOutput?.status === "failed");
  const deployInProgress = Boolean(
    deployInstanceId && !deploySucceeded && !deployFailed && !deployStatusQuery.isError,
  );
  const deployIsActive = startingDeploy || deployInProgress || deploySucceeded;
  const deployState: "idle" | "deploying" | "waiting" | "error" = startingDeploy
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
  }, [deployFailed, deployOutput, deployStatusQuery.data?.runtime_status, storageKey]);

  useEffect(() => {
    if (!deployInstanceId || !deploySucceeded) return;
    if (now - deployStartedAt <= DEPLOY_DISCOVERY_TIMEOUT_MS) return;
    setDeployInstanceId(null);
    clearStoredDeploy(storageKey);
  }, [deployInstanceId, deployStartedAt, deploySucceeded, now, storageKey]);

  useEffect(() => {
    if (!deployStatusQuery.isError || !deployInstanceId) return;
    setDeployError("Previous OpenAPI deploy status could not be restored. Start deploy again.");
    setDeployInstanceId(null);
    clearStoredDeploy(storageKey);
  }, [deployInstanceId, deployStatusQuery.isError, storageKey]);

  // Stale-PENDING guard: Celery's AsyncResult returns "PENDING" both for
  // "task hasn't started yet" and "task id is unknown to the backend". If we
  // sit on Pending for STALE_PENDING_TIMEOUT_MS without ever transitioning to
  // Running, treat the task as lost so the user can deploy again.
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

  const isUpdate = variant === "update";

  return (
    <div
      style={{
        background: "var(--bg-primary)",
        border: isUpdate
          ? "1px solid var(--border-weak)"
          : "1px solid rgba(242,153,74,0.2)",
        borderRadius: 10,
        padding: isUpdate ? "14px 18px" : "20px 24px",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 10,
          marginBottom: 8,
        }}
      >
        {isUpdate ? (
          <RotateCw size={14} style={{ color: "var(--accent)" }} />
        ) : (
          <AlertTriangle size={16} style={{ color: "var(--warning)" }} />
        )}
        <span style={{ fontWeight: 600, fontSize: isUpdate ? 13 : 14 }}>
          {isUpdate ? "Update OpenAPI service" : "OpenAPI service not found"}
        </span>
        {isUpdate && pinnedTag && (
          <span
            style={{
              fontSize: 10,
              padding: "2px 8px",
              borderRadius: 10,
              background: "var(--bg-tertiary)",
              color: "var(--text-faint)",
              fontFamily: "var(--font-mono)",
              fontWeight: 600,
            }}
            title={
              currentTag && currentTag !== pinnedTag
                ? `Running tag may differ — latest in ACR: ${currentTag}`
                : "Tag pinned in this dashboard"
            }
          >
            v{pinnedTag}
          </span>
        )}
      </div>
      {!isUpdate && (
        <p style={{ color: "var(--text-muted)", fontSize: 12, margin: "0 0 12px" }}>
          The{" "}
          <code
            style={{
              fontFamily: "var(--font-mono)",
              background: "var(--bg-tertiary)",
              padding: "1px 5px",
              borderRadius: 3,
            }}
          >
            elb-openapi
          </code>{" "}
          service is not running on <strong>{clusterName || "the cluster"}</strong>. Deploy
          it now to load the live API specification.
        </p>
      )}
      {isUpdate && (
        <p style={{ color: "var(--text-muted)", fontSize: 11, margin: "0 0 10px" }}>
          Re-roll the <code style={{ fontFamily: "var(--font-mono)" }}>elb-openapi</code>{" "}
          deployment with the tag pinned in this dashboard. Use this after the sibling
          <code style={{ fontFamily: "var(--font-mono)", marginLeft: 4 }}>elastic-blast-azure</code>{" "}
          repo bumps the image. The pod is recreated with{" "}
          <code style={{ fontFamily: "var(--font-mono)" }}>imagePullPolicy: Always</code>.
        </p>
      )}

      {!imageBuilt && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "8px 12px",
            marginBottom: 12,
            background: "rgba(184,119,217,0.08)",
            border: "1px solid rgba(184,119,217,0.2)",
            borderRadius: 6,
            fontSize: 11,
            color: "var(--text-muted)",
          }}
        >
          <Package size={12} style={{ color: "var(--purple)" }} />
          The <code style={{ fontFamily: "var(--font-mono)" }}>elb-openapi</code> image
          must be built first — open the ACR card on the Dashboard.
        </div>
      )}

      {deployState === "waiting" && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "10px 14px",
            marginBottom: 12,
            background: "rgba(122,167,255,0.06)",
            border: "1px solid rgba(122,167,255,0.2)",
            borderRadius: 6,
            fontSize: 12,
            color: "var(--accent)",
          }}
        >
          <Loader2 size={13} className="spin" />
          <span>
            Deployed — waiting for pod to start ({waitElapsed}s).
            {waitElapsed < 30 && " This usually takes 30–90 seconds."}
            {waitElapsed >= 30 && waitElapsed < 90 && " Almost there..."}
            {waitElapsed >= 90 &&
              " Taking longer than usual — the pod may be pulling the image."}
          </span>
        </div>
      )}

      {deployState === "deploying" && deployInstanceId && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "10px 14px",
            marginBottom: 12,
            background: "rgba(122,167,255,0.06)",
            border: "1px solid rgba(122,167,255,0.2)",
            borderRadius: 6,
            fontSize: 12,
            color: "var(--accent)",
          }}
        >
          <Loader2 size={13} className="spin" />
          <span>
            {formatDeployPhase(deployCustomStatus?.phase)} ({waitElapsed}s)
          </span>
        </div>
      )}

      {deployState === "error" && deployError && (
        <div
          style={{
            display: "flex",
            alignItems: "flex-start",
            gap: 8,
            padding: "8px 12px",
            marginBottom: 12,
            background: "rgba(242,114,111,0.08)",
            border: "1px solid rgba(242,114,111,0.2)",
            borderRadius: 6,
            fontSize: 11,
            color: "var(--danger)",
          }}
        >
          <AlertTriangle size={12} style={{ flexShrink: 0, marginTop: 1 }} />
          <span style={{ wordBreak: "break-word" }}>{deployError}</span>
        </div>
      )}

      <div
        style={{
          display: "flex",
          gap: 8,
          alignItems: "center",
          flexWrap: "wrap",
        }}
      >
        <button
          type="button"
          className="glass-button glass-button--primary"
          onClick={handleDeploy}
          disabled={!canDeploy}
          title={
            !imageBuilt
              ? "Build the elb-openapi image first"
              : !acrName
                ? "ACR is not configured"
                : isUpdate
                  ? "Update elb-openapi to the pinned tag"
                  : "Deploy elb-openapi to AKS"
          }
          style={{ fontSize: 12 }}
        >
          {deployState === "deploying" ? (
            <>
              <Loader2 size={12} className="spin" /> {isUpdate ? "Updating..." : "Deploying..."}
            </>
          ) : deployState === "waiting" ? (
            <>
              <Loader2 size={12} className="spin" /> Waiting ({waitElapsed}s)
            </>
          ) : isUpdate ? (
            <>
              <RotateCw size={12} /> Update {pinnedTag ? `to v${pinnedTag}` : "now"}
            </>
          ) : (
            <>
              <Rocket size={12} /> Deploy elb-openapi
            </>
          )}
        </button>
        <button
          type="button"
          className="glass-button"
          onClick={onRetry}
          disabled={retrying}
          style={{ fontSize: 12 }}
        >
          <RefreshCw size={12} className={retrying ? "spin" : ""} /> Retry Discovery
        </button>
        {deployInstanceId && !deploySucceeded && (
          <button
            type="button"
            className="glass-button"
            onClick={handleCancelTracking}
            title="Stop tracking this deploy task and re-enable the Deploy button. Does not stop the underlying Celery task if it is actually running."
            style={{ fontSize: 12 }}
          >
            <X size={12} /> Cancel
          </button>
        )}
      </div>
    </div>
  );
}
