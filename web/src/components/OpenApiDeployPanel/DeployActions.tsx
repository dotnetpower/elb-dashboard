import { Hammer, Loader2, RefreshCw, Rocket, RotateCw, X } from "lucide-react";

import type { DeployState } from "./useDeployTask";

export interface DeployActionsProps {
  isUpdate: boolean;
  deployState: DeployState;
  canDeploy: boolean;
  imageBuilt: boolean;
  acrName: string;
  pinnedTag?: string;
  waitElapsed: number;
  deployInstanceId: string | null;
  deploySucceeded: boolean;
  retrying: boolean;
  onDeploy: () => void;
  onRetry: () => void;
  onCancelTracking: () => void;
  /** Rebuild-and-redeploy (build the pinned image then deploy). */
  canRebuild: boolean;
  rebuildInProgress: boolean;
  rebuildPhase: string | null;
  onRebuildDeploy: () => void;
}

export function DeployActions({
  isUpdate,
  deployState,
  canDeploy,
  imageBuilt,
  acrName,
  pinnedTag,
  waitElapsed,
  deployInstanceId,
  deploySucceeded,
  retrying,
  onDeploy,
  onRetry,
  onCancelTracking,
  canRebuild,
  rebuildInProgress,
  rebuildPhase,
  onRebuildDeploy,
}: DeployActionsProps) {
  return (
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
        onClick={onDeploy}
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
            <Loader2 size={12} className="spin" />{" "}
            {isUpdate ? "Updating..." : "Deploying..."}
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
        onClick={onRebuildDeploy}
        disabled={!canRebuild}
        title={
          !acrName
            ? "ACR is not configured"
            : "Build the pinned elb-openapi image in ACR, then redeploy it (one action)"
        }
        style={{ fontSize: 12 }}
      >
        {rebuildInProgress ? (
          <>
            <Loader2 size={12} className="spin" /> Building
            {rebuildPhase ? ` (${rebuildPhase})` : "..."}
          </>
        ) : (
          <>
            <Hammer size={12} /> Rebuild &amp; Deploy
            {pinnedTag ? ` v${pinnedTag}` : ""}
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
        <RefreshCw size={12} className={retrying ? "spin" : ""} /> Retry
        Discovery
      </button>
      {deployInstanceId && !deploySucceeded && (
        <button
          type="button"
          className="glass-button"
          onClick={onCancelTracking}
          title="Revoke the running deploy_openapi_service Celery task on the server and re-enable the Deploy button. The worker honours SIGTERM at the next probe yield (~10 s)."
          style={{ fontSize: 12 }}
        >
          <X size={12} /> Cancel
        </button>
      )}
    </div>
  );
}
