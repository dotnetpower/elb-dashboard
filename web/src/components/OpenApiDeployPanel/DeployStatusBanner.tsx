import { AlertTriangle, Loader2, Package } from "lucide-react";

import { RepairPeeringButton } from "@/pages/apiReference/RepairPeeringButton";

import { formatDeployPhase } from "./storageHelpers";
import type { DeployState } from "./useDeployTask";

export interface ImageNotBuiltBannerProps {
  imageBuilt: boolean;
}

export function ImageNotBuiltBanner({ imageBuilt }: ImageNotBuiltBannerProps) {
  if (imageBuilt) return null;
  return (
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
      The <code style={{ fontFamily: "var(--font-mono)" }}>elb-openapi</code> image must
      be built first — open the ACR card on the Dashboard.
    </div>
  );
}

export interface DeployStatusBannerProps {
  deployState: DeployState;
  deployInstanceId: string | null;
  deployError: string | null;
  deployCustomStatus: { phase?: string } | null | undefined;
  waitElapsed: number;
  /** Envelope-root ``recovery_action`` from the deploy-status response.
   *  When equal to ``"peer_with_platform"`` the banner additionally
   *  renders {@link RepairPeeringButton} so the operator can fix the
   *  VNet peering without leaving the panel. */
  deployRecoveryAction?: string | null;
  deployRecoveryHint?: string | null;
  /** Required when {@link deployRecoveryAction} is set so the Repair
   *  button can target the right cluster. */
  subscriptionId?: string;
  resourceGroup?: string;
  clusterName?: string;
  /** Called after the Repair button succeeds so the panel can refetch
   *  service discovery. */
  onRecoveryResolved?: () => void;
}

export function DeployStatusBanner({
  deployState,
  deployInstanceId,
  deployError,
  deployCustomStatus,
  waitElapsed,
  deployRecoveryAction,
  deployRecoveryHint,
  subscriptionId,
  resourceGroup,
  clusterName,
  onRecoveryResolved,
}: DeployStatusBannerProps) {
  if (deployState === "waiting") {
    return (
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
          Deployment ready — refreshing service discovery ({waitElapsed}s).
          {waitElapsed < 30 && " The ready pod is already verified."}
          {waitElapsed >= 30 &&
            waitElapsed < 90 &&
            " Waiting for the dashboard to observe the service."}
          {waitElapsed >= 90 &&
            " Taking longer than usual — refresh the cluster detail if the endpoint is still missing."}
        </span>
      </div>
    );
  }
  if (deployState === "deploying" && deployInstanceId) {
    return (
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
    );
  }
  if (deployState === "error" && deployError) {
    const showRepair =
      deployRecoveryAction === "peer_with_platform" &&
      Boolean(subscriptionId && resourceGroup && clusterName);
    return (
      <div
        style={{
          display: "flex",
          flexDirection: "column",
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
        <div style={{ display: "flex", alignItems: "flex-start", gap: 8 }}>
          <AlertTriangle size={12} style={{ flexShrink: 0, marginTop: 1 }} />
          <span style={{ wordBreak: "break-word" }}>{deployError}</span>
        </div>
        {showRepair && (
          <>
            {deployRecoveryHint && (
              <div
                style={{
                  fontSize: 11,
                  color: "var(--text-muted)",
                  marginLeft: 20,
                }}
              >
                {deployRecoveryHint}
              </div>
            )}
            <div style={{ marginLeft: 20 }}>
              <RepairPeeringButton
                subscriptionId={subscriptionId!}
                resourceGroup={resourceGroup!}
                clusterName={clusterName!}
                onResolved={() => onRecoveryResolved?.()}
                size="compact"
              />
            </div>
          </>
        )}
      </div>
    );
  }
  return null;
}
