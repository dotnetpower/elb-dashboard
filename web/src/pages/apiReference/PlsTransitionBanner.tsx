import { useQuery } from "@tanstack/react-query";
import { AlertTriangle } from "lucide-react";

import { aksApi } from "@/api/endpoints";

/**
 * Render a single yellow banner when the deploy environment has PLS enabled
 * (``OPENAPI_PLS_ENABLED=1``) but the live ``elb-openapi`` Service is missing
 * the ``service.beta.kubernetes.io/azure-pls-create`` annotation.
 *
 * In that state the AKS LB controller will not retro-actively attach a PLS
 * to the existing Service — the deploy task refuses to re-apply without
 * ``OPENAPI_PLS_CONFIRM_RECREATE=1`` because Service re-creation drops the
 * external IP briefly. Surfacing this prevents operators from clicking
 * "Deploy" repeatedly while nothing changes.
 *
 * Hidden when the probe is unavailable (RBAC missing / k8s unreachable),
 * when PLS is not enabled, or when the annotation is already in place.
 */
export function PlsTransitionBanner({
  subscriptionId,
  resourceGroup,
  clusterName,
}: {
  subscriptionId: string;
  resourceGroup: string;
  clusterName: string;
}) {
  const query = useQuery({
    queryKey: ["openapi-pls", subscriptionId, resourceGroup, clusterName],
    queryFn: () => aksApi.openApiPls(subscriptionId, resourceGroup, clusterName),
    enabled: Boolean(subscriptionId && resourceGroup && clusterName),
    staleTime: 30_000,
    retry: 1,
  });

  const status = query.data;
  if (!status || !status.available) return null;
  if (!status.transition_pending) return null;

  return (
    <div
      className="glass-card"
      role="alert"
      aria-live="polite"
      style={{
        borderColor: "var(--warning-border, rgba(255, 196, 0, 0.5))",
        background: "var(--warning-surface, rgba(255, 196, 0, 0.08))",
        padding: 16,
        display: "flex",
        gap: 12,
        alignItems: "flex-start",
      }}
    >
      <AlertTriangle size={18} strokeWidth={1.5} aria-hidden />
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        <strong>Private Link Service transition pending</strong>
        <span style={{ fontSize: 13, opacity: 0.85 }}>
          The deploy environment enables PLS ({status.pls_name || "elb-openapi-pls"}), but the
          live <code>elb-openapi</code> Service does not yet carry the
          <code> azure-pls-create</code> annotation. The next deploy must re-create the Service
          to attach the PLS — set <code>OPENAPI_PLS_CONFIRM_RECREATE=1</code> on the api sidecar
          and re-run the deploy task.
        </span>
      </div>
    </div>
  );
}
