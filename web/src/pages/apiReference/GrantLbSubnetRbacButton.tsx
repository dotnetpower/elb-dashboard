import { useMutation } from "@tanstack/react-query";
import { CheckCircle2, Loader2, ShieldCheck } from "lucide-react";

import { aksApi } from "@/api/endpoints";
import { formatApiError } from "@/api/client";
import type { AksLbSubnetRbacResponse } from "@/api/aks";

/** Inline affordance for the "elb-openapi internal LoadBalancer stuck
 *  <pending> → cluster identity missing Network Contributor on its BYO node
 *  subnet" recovery path (GitHub #33). Renders a single button that POSTs to
 *  `/api/aks/openapi/lb-subnet-rbac` (the same idempotent grant the AKS
 *  provision / deploy tasks perform) and surfaces success / failure inline.
 *  On success the caller's `onResolved` should refetch whatever produced the
 *  degraded payload.
 *
 *  Disabled when `clusterName` or `resourceGroup` is empty — the grant needs
 *  both to resolve the cluster identity + subnet. */
export function GrantLbSubnetRbacButton({
  subscriptionId,
  resourceGroup,
  clusterName,
  onResolved,
  size = "compact",
}: {
  subscriptionId: string;
  resourceGroup: string;
  clusterName: string;
  onResolved?: (result: AksLbSubnetRbacResponse) => void;
  /** `compact` = inline next to an error message; `block` = standalone panel. */
  size?: "compact" | "block";
}) {
  const mutation = useMutation({
    mutationFn: () =>
      aksApi.grantLbSubnetRbac(subscriptionId, resourceGroup, clusterName),
    onSuccess: (data) => {
      // `granted` and `skipped` are both non-error outcomes. The LB may still
      // take a few minutes to pick up the role (token-cache), so we do not
      // claim instant success — the caller refetches and the degraded state
      // clears on its own once the IP lands.
      if (data.status !== "error") onResolved?.(data);
    },
  });

  const disabled =
    mutation.isPending || !subscriptionId || !resourceGroup || !clusterName;

  const resultStatus = mutation.data?.status;
  const resultGranted = resultStatus === "granted";
  const resultSkipped = resultStatus === "skipped";
  const errMessage = mutation.error
    ? formatApiError(mutation.error, "aks")
    : resultStatus === "error"
      ? (mutation.data?.error ?? "grant failed")
      : null;

  const buttonLabel = mutation.isPending
    ? "Granting…"
    : resultGranted
      ? "RBAC granted"
      : "Grant LB subnet RBAC";

  const buttonIcon = mutation.isPending ? (
    <Loader2 size={12} className="spin" />
  ) : resultGranted ? (
    <CheckCircle2 size={12} />
  ) : (
    <ShieldCheck size={12} />
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
          Could not grant subnet RBAC: {errMessage}.
        </span>
      )}
      {resultSkipped && (
        <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
          Not applicable here ({mutation.data?.reason ?? "skipped"}).
        </span>
      )}
      {resultGranted && (
        <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
          {mutation.data?.note ??
            "Role granted. The LoadBalancer may take a few minutes to pick it up."}
        </span>
      )}
    </div>
  );
}

/** Heuristic: does this thrown error or response payload look like the
 *  "elb-openapi LoadBalancer pending → node-subnet RBAC missing" recovery
 *  path (GitHub #33)? Reads the canonical
 *  `recovery_action === "grant_lb_subnet_rbac"` field the backend emits on the
 *  spec / proxy degraded payload. Accepts unknown to keep call sites unfussy. */
export function isGrantLbSubnetRbacRecovery(payload: unknown): boolean {
  if (!payload || typeof payload !== "object") return false;
  const record = payload as { recovery_action?: unknown; body?: unknown };
  if (record.recovery_action === "grant_lb_subnet_rbac") return true;
  if (record.body) return isGrantLbSubnetRbacRecovery(record.body);
  return false;
}
