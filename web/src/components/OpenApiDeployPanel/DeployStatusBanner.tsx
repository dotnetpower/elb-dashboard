import { AlertTriangle, Loader2, Package } from "lucide-react";

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
      The <code style={{ fontFamily: "var(--font-mono)" }}>elb-openapi</code>{" "}
      image must be built first — open the ACR card on the Dashboard.
    </div>
  );
}

export interface DeployStatusBannerProps {
  deployState: DeployState;
  deployInstanceId: string | null;
  deployError: string | null;
  deployCustomStatus: { phase?: string } | null | undefined;
  waitElapsed: number;
}

export function DeployStatusBanner({
  deployState,
  deployInstanceId,
  deployError,
  deployCustomStatus,
  waitElapsed,
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
          Deployed — waiting for pod to start ({waitElapsed}s).
          {waitElapsed < 30 && " This usually takes 30–90 seconds."}
          {waitElapsed >= 30 && waitElapsed < 90 && " Almost there..."}
          {waitElapsed >= 90 &&
            " Taking longer than usual — the pod may be pulling the image."}
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
    return (
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
    );
  }
  return null;
}
