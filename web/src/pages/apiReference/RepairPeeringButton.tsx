import { useMutation } from "@tanstack/react-query";
import { CheckCircle2, Loader2, Wrench } from "lucide-react";

import { aksApi } from "@/api/endpoints";
import { formatApiError } from "@/api/client";
import type { AksPeerWithPlatformResponse } from "@/api/aks";

/** Inline affordance for the "elb-openapi unreachable → VNet peering missing"
 *  recovery path. Renders a single button that POSTs to
 *  `/api/aks/peer-with-platform` (the same idempotent helper the AKS
 *  provision task runs at the end of cluster create) and surfaces success /
 *  failure inline. On success the caller's `onResolved` callback should
 *  refetch whatever query produced the upstream-unreachable error.
 *
 *  Disabled when `clusterName` or `resourceGroup` is empty — the recovery
 *  endpoint needs both to resolve the AKS auto-VNet, and there is no useful
 *  fallback (the operator must pick a cluster first). */
export function RepairPeeringButton({
  subscriptionId,
  resourceGroup,
  clusterName,
  onResolved,
  size = "compact",
}: {
  subscriptionId: string;
  resourceGroup: string;
  clusterName: string;
  onResolved?: (result: AksPeerWithPlatformResponse) => void;
  /** `compact` = inline next to an error message; `block` = standalone panel. */
  size?: "compact" | "block";
}) {
  const mutation = useMutation({
    mutationFn: () =>
      aksApi.peerWithPlatform(subscriptionId, resourceGroup, clusterName),
    onSuccess: (data) => {
      // The helper returns 200 even when one direction failed — the SPA
      // surfaces that via `result.error` rather than throwing. Treat
      // skipped (BYO-VNet, env not resolved) as resolved-but-no-op too.
      if (!data.error) onResolved?.(data);
    },
  });

  const disabled =
    mutation.isPending ||
    !subscriptionId ||
    !resourceGroup ||
    !clusterName;

  const resultError = mutation.data?.error;
  const resultSkipped = mutation.data?.skipped;
  const resultOk = Boolean(mutation.data && !resultError && !resultSkipped);
  const errMessage = mutation.error
    ? formatApiError(mutation.error, "aks")
    : resultError
      ? resultError
      : null;

  const buttonLabel = mutation.isPending
    ? "Repairing…"
    : resultOk
      ? "Peering restored"
      : "Repair VNet peering";

  const buttonIcon = mutation.isPending ? (
    <Loader2 size={12} className="spin" />
  ) : resultOk ? (
    <CheckCircle2 size={12} />
  ) : (
    <Wrench size={12} />
  );

  return (
    <div
      style={{
        display: "flex",
        flexDirection: size === "block" ? "column" : "row",
        alignItems: size === "block" ? "flex-start" : "center",
        gap: 8,
        marginTop: 8,
      }}
    >
      <button
        type="button"
        className="glass-button glass-button--primary"
        onClick={() => mutation.mutate()}
        disabled={disabled}
        style={{ fontSize: 11, gap: 5, padding: "5px 14px" }}
      >
        {buttonIcon}
        {buttonLabel}
      </button>
      {errMessage && (
        <span style={{ fontSize: 11, color: "var(--danger)" }}>
          Could not repair peering: {errMessage}. Run{" "}
          <code style={{ fontFamily: "var(--font-mono)" }}>
            scripts/dev/peer-cluster-network.sh
          </code>{" "}
          as an admin.
        </span>
      )}
      {resultSkipped && (
        <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
          Peering not applicable here ({mutation.data?.reason ?? "skipped"}).
        </span>
      )}
      {resultOk && (
        <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
          Both directions Connected. Re-fetching…
        </span>
      )}
    </div>
  );
}

/** Heuristic: does this thrown error or response payload look like the
 *  "elb-openapi unreachable → VNet peering missing" recovery path? Reads
 *  both the canonical `recovery_action === "peer_with_platform"` field
 *  (preferred) and the legacy `code` / `degraded_reason` strings for
 *  backward compatibility with deployments that have not yet rolled out
 *  the enriched payload. Accepts unknown to keep call sites unfussy. */
export function isPeerWithPlatformRecovery(payload: unknown): boolean {
  if (!payload || typeof payload !== "object") return false;
  const record = payload as {
    recovery_action?: unknown;
    code?: unknown;
    degraded_reason?: unknown;
    body?: unknown;
  };
  if (record.recovery_action === "peer_with_platform") return true;
  if (
    typeof record.code === "string" &&
    (record.code === "openapi_upstream_unreachable" ||
      record.code === "openapi_service_not_reachable")
  ) {
    return true;
  }
  if (
    typeof record.degraded_reason === "string" &&
    (record.degraded_reason === "openapi_endpoint_unreachable" ||
      record.degraded_reason === "openapi_service_not_reachable")
  ) {
    return true;
  }
  // Thrown ApiError carries the parsed JSON body on `.body`.
  if (record.body) return isPeerWithPlatformRecovery(record.body);
  return false;
}
