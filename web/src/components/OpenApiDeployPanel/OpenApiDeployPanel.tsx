import { DeployActions } from "./DeployActions";
import { DeployHeader } from "./DeployHeader";
import {
  DeployStatusBanner,
  ImageNotBuiltBanner,
} from "./DeployStatusBanner";
import { useDeployTask } from "./useDeployTask";

export interface OpenApiDeployPanelProps {
  subscriptionId: string;
  resourceGroup: string;
  clusterName: string;
  acrName: string;
  acrResourceGroup: string;
  storageAccount: string;
  storageResourceGroup: string;
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

export function OpenApiDeployPanel({
  subscriptionId,
  resourceGroup,
  clusterName,
  acrName,
  acrResourceGroup,
  storageAccount,
  storageResourceGroup,
  imageBuilt,
  onRetry,
  retrying,
  variant = "deploy",
  pinnedTag,
  currentTag,
}: OpenApiDeployPanelProps) {
  const {
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
  } = useDeployTask({
    subscriptionId,
    resourceGroup,
    clusterName,
    acrName,
    acrResourceGroup,
    storageAccount,
    storageResourceGroup,
    imageBuilt,
    onRetry,
  });

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
      <DeployHeader
        isUpdate={isUpdate}
        clusterName={clusterName}
        pinnedTag={pinnedTag}
        currentTag={currentTag}
      />

      <ImageNotBuiltBanner imageBuilt={imageBuilt} />

      <DeployStatusBanner
        deployState={deployState}
        deployInstanceId={deployInstanceId}
        deployError={deployError}
        deployCustomStatus={deployCustomStatus}
        waitElapsed={waitElapsed}
        deployRecoveryAction={deployRecoveryAction}
        deployRecoveryHint={deployRecoveryHint}
        subscriptionId={subscriptionId}
        resourceGroup={resourceGroup}
        clusterName={clusterName}
        onRecoveryResolved={onRetry}
      />

      <DeployActions
        isUpdate={isUpdate}
        deployState={deployState}
        canDeploy={canDeploy}
        imageBuilt={imageBuilt}
        acrName={acrName}
        pinnedTag={pinnedTag}
        waitElapsed={waitElapsed}
        deployInstanceId={deployInstanceId}
        deploySucceeded={deploySucceeded}
        retrying={retrying}
        onDeploy={handleDeploy}
        onRetry={onRetry}
        onCancelTracking={handleCancelTracking}
      />
    </div>
  );
}
