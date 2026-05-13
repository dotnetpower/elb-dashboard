import { useState } from "react";
import { Loader2, AlertTriangle, RefreshCw, Package, Rocket } from "lucide-react";

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
}: Props) {
  const [deployState, setDeployState] = useState<
    "idle" | "deploying" | "waiting" | "error"
  >("idle");
  const [deployError, setDeployError] = useState<string | null>(null);
  const [waitElapsed, setWaitElapsed] = useState(0);

  const canDeploy =
    Boolean(subscriptionId && resourceGroup && clusterName && acrName) &&
    imageBuilt &&
    deployState !== "deploying" &&
    deployState !== "waiting";

  const handleDeploy = async () => {
    setDeployState("deploying");
    setDeployError(null);
    setWaitElapsed(0);
    try {
      await aksApi.deployOpenApi(
        subscriptionId,
        resourceGroup,
        clusterName,
        acrName,
        storageAccount,
      );
      setDeployState("waiting");
      const start = Date.now();
      const timer = setInterval(() => {
        setWaitElapsed(Math.floor((Date.now() - start) / 1000));
      }, 1000);
      const poll = () => {
        if (Date.now() - start > 180_000) {
          clearInterval(timer);
          return;
        }
        onRetry();
        setTimeout(poll, 10_000);
      };
      setTimeout(poll, 15_000);
    } catch (err: unknown) {
      setDeployState("error");
      setDeployError(formatApiError(err));
    }
  };

  return (
    <div
      style={{
        background: "var(--bg-primary)",
        border: "1px solid rgba(242,153,74,0.2)",
        borderRadius: 10,
        padding: "20px 24px",
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
        <AlertTriangle size={16} style={{ color: "var(--warning)" }} />
        <span style={{ fontWeight: 600, fontSize: 14 }}>OpenAPI service not found</span>
      </div>
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
          className="glass-button glass-button--primary"
          onClick={handleDeploy}
          disabled={!canDeploy}
          title={
            !imageBuilt
              ? "Build the elb-openapi image first"
              : !acrName
                ? "ACR is not configured"
                : "Deploy elb-openapi to AKS"
          }
          style={{ fontSize: 12 }}
        >
          {deployState === "deploying" ? (
            <>
              <Loader2 size={12} className="spin" /> Deploying...
            </>
          ) : deployState === "waiting" ? (
            <>
              <Loader2 size={12} className="spin" /> Waiting ({waitElapsed}s)
            </>
          ) : (
            <>
              <Rocket size={12} /> Deploy elb-openapi
            </>
          )}
        </button>
        <button
          className="glass-button"
          onClick={onRetry}
          disabled={retrying}
          style={{ fontSize: 12 }}
        >
          <RefreshCw size={12} className={retrying ? "spin" : ""} /> Retry Discovery
        </button>
      </div>
    </div>
  );
}
