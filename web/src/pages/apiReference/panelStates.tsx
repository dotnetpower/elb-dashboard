/**
 * API Reference page — empty / loading / error / degraded panel states.
 *
 * Every non-happy-path surface the API Reference page shows before the
 * live spec renders: missing config, cluster picker, cluster stopped,
 * missing OpenAPI image, manifest-drift diagnostics, spec error (with
 * peering/RBAC repair affordances), and the pod-starting poll state.
 * Shared primitives (`PanelState`, `StateIcon`, `InlineCode`) live here
 * too. Pure presentation; the page owns the data + handlers.
 */

import { useMemo } from "react";
import type { CSSProperties, ReactNode } from "react";
import { AlertTriangle, Loader2, Package, Power, RefreshCw, Server } from "lucide-react";
import { Link } from "react-router-dom";

import type { AksClusterSummary } from "@/api/endpoints";
import { isAksWorkloadReady } from "@/utils/aksStatus";
import { GrantLbSubnetRbacButton } from "@/pages/apiReference/GrantLbSubnetRbacButton";
import { RepairPeeringButton } from "@/pages/apiReference/RepairPeeringButton";
import type { OpenApiSpecDegraded } from "@/pages/apiReference/openApiPodStartup";
import { ApiReferenceSkeleton } from "@/pages/apiReference/skeletons";

export function OpenApiLoadingState() {
  return <ApiReferenceSkeleton label="Discovering OpenAPI service on AKS" />;
}

export function MissingConfigState() {
  return (
    <PanelState border="1px solid var(--border-weak)" textAlign="center">
      <AlertTriangle size={20} style={{ color: "var(--warning)", marginBottom: 8 }} />
      <p style={{ color: "var(--text-muted)", fontSize: 13 }}>
        Configure Subscription and Workload RG in the Dashboard first.
      </p>
    </PanelState>
  );
}

export function ClusterPicker({
  clusters,
  selectedName,
  onSelect,
}: {
  clusters: AksClusterSummary[];
  selectedName: string;
  onSelect: (name: string) => void;
}) {
  // Issues-first ordering so the running cluster surfaces above the
  // stopped one. Mirrors the Dashboard's ClusterCard sort so the user
  // sees the same ordering on both surfaces.
  const sorted = useMemo(() => {
    const bucket = (c: AksClusterSummary): number => {
      if (isAksWorkloadReady(c)) return 0;
      if (c.power_state === "Stopped") return 2;
      return 1;
    };
    return [...clusters].sort((a, b) => {
      const ba = bucket(a);
      const bb = bucket(b);
      if (ba !== bb) return ba - bb;
      return a.name.localeCompare(b.name);
    });
  }, [clusters]);

  return (
    <PanelState border="1px solid var(--border-weak)">
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 10,
          flexWrap: "wrap",
        }}
      >
        <Server size={14} style={{ color: "var(--text-faint)" }} />
        <span
          style={{
            fontSize: 11,
            fontWeight: 600,
            textTransform: "uppercase",
            letterSpacing: "0.08em",
            color: "var(--text-faint)",
          }}
        >
          Cluster
        </span>
        <div
          role="radiogroup"
          aria-label="Select an AKS cluster for the OpenAPI service"
          style={{ display: "flex", gap: 6, flexWrap: "wrap" }}
        >
          {sorted.map((c) => {
            const running = isAksWorkloadReady(c);
            const isSelected = c.name === selectedName;
            const dotColor = running
              ? "var(--success)"
              : c.power_state === "Stopped"
                ? "var(--danger)"
                : "var(--text-faint)";
            return (
              <button
                key={`${c.resource_group}/${c.name}`}
                type="button"
                role="radio"
                aria-checked={isSelected}
                onClick={() => onSelect(c.name)}
                title={`${c.name} (${c.resource_group}) · ${
                  running ? "Running" : c.power_state ?? "Unknown"
                }`}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 6,
                  padding: "4px 10px",
                  fontSize: 12,
                  fontWeight: isSelected ? 600 : 500,
                  color: isSelected
                    ? "var(--text-primary)"
                    : "var(--text-secondary)",
                  background: isSelected
                    ? "rgba(122,167,255,0.12)"
                    : "transparent",
                  border: `1px solid ${
                    isSelected ? "var(--accent)" : "var(--border-medium)"
                  }`,
                  borderRadius: 6,
                  cursor: "pointer",
                  lineHeight: 1.2,
                }}
              >
                <span
                  aria-hidden="true"
                  style={{
                    width: 6,
                    height: 6,
                    borderRadius: "50%",
                    background: dotColor,
                    flexShrink: 0,
                  }}
                />
                {c.name}
                {!running && (
                  <span
                    style={{
                      fontSize: 10,
                      color: "var(--text-faint)",
                      fontWeight: 500,
                    }}
                  >
                    ({c.power_state ?? "stopped"})
                  </span>
                )}
              </button>
            );
          })}
        </div>
        <span
          style={{
            marginLeft: "auto",
            fontSize: 11,
            color: "var(--text-faint)",
          }}
        >
          Selection persists per browser.
        </span>
      </div>
    </PanelState>
  );
}

export function ClusterStoppedState({
  clusterName,
  powerState,
  region,
  refreshing,
  onRefresh,
}: {
  clusterName: string;
  powerState: string;
  region: string;
  refreshing: boolean;
  onRefresh: () => void;
}) {
  return (
    <PanelState border="1px solid rgba(242,153,74,0.2)">
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
        <StateIcon background="rgba(242,153,74,0.1)">
          <Power size={18} style={{ color: "var(--warning)" }} />
        </StateIcon>
        <div>
          <div style={{ fontWeight: 600, fontSize: 14 }}>AKS cluster is stopped</div>
          <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
            The OpenAPI service runs inside the AKS cluster. Start the cluster to access
            the API.
          </div>
        </div>
      </div>
      <div
        style={{
          display: "flex",
          gap: 8,
          flexWrap: "wrap",
          padding: "12px 16px",
          background: "var(--bg-secondary)",
          borderRadius: 8,
          fontSize: 12,
          color: "var(--text-muted)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <Server size={12} style={{ color: "var(--text-faint)" }} />
          <span>{clusterName}</span>
        </div>
        <span style={{ color: "var(--border-medium)" }}>·</span>
        <span style={{ color: "var(--warning)", fontWeight: 600 }}>{powerState}</span>
        <span style={{ color: "var(--border-medium)" }}>·</span>
        <span>{region}</span>
      </div>
      <div style={{ marginTop: 12, display: "flex", gap: 8, alignItems: "center" }}>
        <Link
          to="/"
          className="glass-button glass-button--primary"
          style={{ fontSize: 12, textDecoration: "none" }}
        >
          <Power size={12} /> Go to Dashboard to start cluster
        </Link>
        <button
          className="glass-button"
          onClick={onRefresh}
          disabled={refreshing}
          style={{ fontSize: 12 }}
        >
          <RefreshCw size={12} className={refreshing ? "spin" : ""} /> Refresh
        </button>
      </div>
    </PanelState>
  );
}

export function MissingOpenApiImageState() {
  return (
    <PanelState border="1px solid rgba(184,119,217,0.2)">
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
        <StateIcon background="rgba(184,119,217,0.1)">
          <Package size={18} style={{ color: "var(--purple)" }} />
        </StateIcon>
        <div>
          <div style={{ fontWeight: 600, fontSize: 14 }}>OpenAPI image not built</div>
          <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
            The <InlineCode>elb-openapi</InlineCode> container image needs to be built in
            your ACR before deploying the API service.
          </div>
        </div>
      </div>
      <Link
        to="/"
        className="glass-button glass-button--primary"
        style={{ fontSize: 12, textDecoration: "none" }}
      >
        <Package size={12} /> Build images from Dashboard ACR card
      </Link>
    </PanelState>
  );
}

export function SpecLoadingState() {
  return <ApiReferenceSkeleton compact label="Loading API specification" />;
}

export function OpenApiManifestDiagnostic({
  reason,
  retrying,
  onRetry,
}: {
  reason: "read_failed" | "signal_missing";
  retrying: boolean;
  onRetry: () => void;
}) {
  const readFailed = reason === "read_failed";
  return (
    <PanelState border="1px solid rgba(242,153,74,0.2)">
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10 }}>
        <StateIcon background="rgba(242,153,74,0.1)">
          <AlertTriangle size={16} style={{ color: "var(--warning)" }} />
        </StateIcon>
        <div>
          <div style={{ fontWeight: 600, fontSize: 13 }}>
            {readFailed
              ? "elb-openapi deployment status unavailable"
              : "Redeploy detection not available yet"}
          </div>
          <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
            {readFailed ? (
              <>
                The dashboard could not read the live{" "}
                <InlineCode>elb-openapi</InlineCode> deployment (workload-cluster
                unreachable or missing kubectl RBAC), so it cannot tell whether a
                redeploy is needed. Resolve cluster access, then refresh.
              </>
            ) : (
              <>
                This control plane (<InlineCode>api</InlineCode> image) predates
                manifest-drift detection, so it never reports whether the live{" "}
                <InlineCode>elb-openapi</InlineCode> manifest is outdated. Redeploy
                the control plane (rebuild + roll the <InlineCode>api</InlineCode>{" "}
                image) to enable the redeploy prompt.
              </>
            )}
          </div>
        </div>
      </div>
      <button
        type="button"
        className="glass-button"
        style={{ fontSize: 12 }}
        onClick={onRetry}
        disabled={retrying}
      >
        <RefreshCw size={12} /> {retrying ? "Refreshing…" : "Refresh"}
      </button>
    </PanelState>
  );
}

export function SpecErrorState({
  message,
  showRepair,
  showGrantRbac,
  subscriptionId,
  resourceGroup,
  clusterName,
  onResolved,
}: {
  message: string;
  showRepair?: boolean;
  showGrantRbac?: boolean;
  subscriptionId?: string;
  resourceGroup?: string;
  clusterName?: string;
  onResolved?: () => void;
}) {
  const hasTarget = Boolean(subscriptionId && resourceGroup && clusterName);
  return (
    <PanelState border="1px solid rgba(242,114,111,0.2)" padding="16px 20px">
      <AlertTriangle
        size={14}
        style={{ color: "var(--danger)", verticalAlign: "middle", marginRight: 6 }}
      />
      <span style={{ fontSize: 12 }}>Failed to load openapi.json: {message}</span>
      {showGrantRbac && hasTarget && (
        <GrantLbSubnetRbacButton
          subscriptionId={subscriptionId!}
          resourceGroup={resourceGroup!}
          clusterName={clusterName!}
          onResolved={() => onResolved?.()}
          size="block"
        />
      )}
      {showRepair && !showGrantRbac && hasTarget && (
        <RepairPeeringButton
          subscriptionId={subscriptionId!}
          resourceGroup={resourceGroup!}
          clusterName={clusterName!}
          onResolved={() => onResolved?.()}
          size="block"
        />
      )}
    </PanelState>
  );
}

export function OpenApiPodStartingState({
  data,
  refreshing,
  onRefresh,
}: {
  data: OpenApiSpecDegraded;
  refreshing: boolean;
  onRefresh: () => void;
}) {
  // `openapi_pod_starting` is benign and self-resolving (the page auto-polls
  // and flips to the live API Reference once the pod serves).
  // `openapi_pod_not_ready` means the pod is up but failing readiness (e.g.
  // CrashLoopBackOff) — still NOT a peering problem, so we surface a muted
  // warning that points at the pod logs rather than the "Repair VNet peering"
  // affordance.
  const failed = data.degraded_reason === "openapi_pod_not_ready";
  const accent = failed ? "var(--warning)" : "var(--accent)";
  const tint = failed ? "rgba(242,153,74,0.1)" : "rgba(122,167,255,0.1)";
  const border = failed
    ? "1px solid rgba(242,153,74,0.2)"
    : "1px solid rgba(122,167,255,0.2)";
  const title = failed ? "elb-openapi pod is not ready" : "elb-openapi is starting";
  const message =
    data.pod_message ??
    (failed
      ? "The elb-openapi pod is up but not passing its readiness check. Check the pod logs."
      : "The elb-openapi pod is starting. This usually finishes within ~2 minutes on a fresh node while the container image is pulled.");
  return (
    <PanelState border={border}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10 }}>
        <StateIcon background={tint}>
          {failed ? (
            <AlertTriangle size={16} style={{ color: accent }} />
          ) : (
            <Loader2 size={16} className="spin" style={{ color: accent }} />
          )}
        </StateIcon>
        <div>
          <div style={{ fontWeight: 600, fontSize: 13 }}>{title}</div>
          <div style={{ fontSize: 12, color: "var(--text-muted)" }}>{message}</div>
        </div>
      </div>
      <button
        type="button"
        className="glass-button"
        style={{ fontSize: 12 }}
        onClick={onRefresh}
        disabled={refreshing}
      >
        <RefreshCw size={12} className={refreshing ? "spin" : ""} />{" "}
        {refreshing ? "Checking…" : "Check again"}
      </button>
    </PanelState>
  );
}

export function PanelState({
  children,
  border,
  padding = "24px 28px",
  textAlign,
}: {
  children: ReactNode;
  border: string;
  padding?: string;
  textAlign?: CSSProperties["textAlign"];
}) {
  return (
    <div
      style={{
        background: "var(--bg-primary)",
        border,
        borderRadius: 10,
        padding,
        textAlign,
      }}
    >
      {children}
    </div>
  );
}

function StateIcon({
  children,
  background,
}: {
  children: ReactNode;
  background: string;
}) {
  return (
    <div
      style={{
        width: 36,
        height: 36,
        borderRadius: 10,
        background,
        display: "grid",
        placeItems: "center",
      }}
    >
      {children}
    </div>
  );
}

function InlineCode({ children }: { children: ReactNode }) {
  return (
    <code
      style={{
        fontFamily: "var(--font-mono)",
        background: "var(--bg-tertiary)",
        padding: "1px 5px",
        borderRadius: 3,
      }}
    >
      {children}
    </code>
  );
}
