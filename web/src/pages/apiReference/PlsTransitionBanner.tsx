import { useMutation, useQuery } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, Loader2, Wrench } from "lucide-react";

import { aksApi } from "@/api/endpoints";
import { formatApiError } from "@/api/client";

// Critique #20.6: replace hand-rolled ``rgba(255, 196, 0, …)`` literals
// with ``color-mix`` against the theme ``--warning`` token so a future
// theme rotation (light mode, high-contrast, etc.) propagates here
// without the banner staying stuck on its hard-coded amber. Exported
// for the unit test so the colour pipeline is locked-in.
export const PLS_BANNER_BORDER_COLOR =
  "color-mix(in srgb, var(--warning) 50%, transparent)";
export const PLS_BANNER_BACKGROUND_COLOR =
  "color-mix(in srgb, var(--warning) 8%, transparent)";

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
 * Issue #22: the banner now ships a ``Deploy with PLS recreate`` button
 * that enqueues the same Celery deploy task with ``confirm_recreate=true``
 * so the operator can recreate the Service without leaving the page or
 * setting an env var on the api sidecar. The button is disabled when the
 * ACR coordinates needed to enqueue the deploy task (``acrName``) are
 * missing — the route returns 400 in that case, so we surface the gap as
 * a disabled-tooltip rather than a confusing 400 toast.
 *
 * Hidden when the probe is unavailable (RBAC missing / k8s unreachable),
 * when PLS is not enabled, or when the annotation is already in place.
 */
export function PlsTransitionBanner({
  subscriptionId,
  resourceGroup,
  clusterName,
  acrName,
  acrResourceGroup,
  storageAccount,
  storageResourceGroup,
}: {
  subscriptionId: string;
  resourceGroup: string;
  clusterName: string;
  /** ACR / Storage coordinates forwarded to ``aksApi.deployOpenApi``.
   *  Optional so legacy callers keep type-checking; when missing the
   *  recreate button is disabled with a hint. */
  acrName?: string;
  acrResourceGroup?: string;
  storageAccount?: string;
  storageResourceGroup?: string;
}) {
  const query = useQuery({
    queryKey: ["openapi-pls", subscriptionId, resourceGroup, clusterName],
    queryFn: () => aksApi.openApiPls(subscriptionId, resourceGroup, clusterName),
    enabled: Boolean(subscriptionId && resourceGroup && clusterName),
    staleTime: 30_000,
    retry: 1,
  });

  const recreate = useMutation({
    mutationFn: () =>
      aksApi.deployOpenApi(
        subscriptionId,
        resourceGroup,
        clusterName,
        acrName,
        storageAccount,
        storageResourceGroup,
        acrResourceGroup,
        true,
      ),
  });

  const status = query.data;
  if (!status || !status.available) return null;
  if (!status.transition_pending) return null;

  const canRecreate =
    Boolean(subscriptionId && resourceGroup && clusterName && acrName) &&
    !recreate.isPending &&
    !recreate.isSuccess;

  const buttonLabel = recreate.isPending
    ? "Enqueuing deploy…"
    : recreate.isSuccess
      ? "Deploy enqueued"
      : "Deploy with PLS recreate";

  const buttonIcon = recreate.isPending ? (
    <Loader2 size={12} className="spin" />
  ) : recreate.isSuccess ? (
    <CheckCircle2 size={12} />
  ) : (
    <Wrench size={12} />
  );

  const errMessage = recreate.error
    ? formatApiError(recreate.error, "aks")
    : null;

  return (
    <div
      className="glass-card"
      role="alert"
      aria-live="polite"
      style={{
        borderColor: PLS_BANNER_BORDER_COLOR,
        background: PLS_BANNER_BACKGROUND_COLOR,
        padding: 16,
        display: "flex",
        gap: 12,
        alignItems: "flex-start",
      }}
    >
      <AlertTriangle size={18} strokeWidth={1.5} aria-hidden />
      <div style={{ display: "flex", flexDirection: "column", gap: 8, flex: 1 }}>
        <strong>Private Link Service transition pending</strong>
        <span style={{ fontSize: 13, opacity: 0.85 }}>
          The deploy environment enables PLS ({status.pls_name || "elb-openapi-pls"}), but the
          live <code>elb-openapi</code> Service does not yet carry the
          <code> azure-pls-create</code> annotation. The next deploy must re-create the Service
          to attach the PLS — this drops the external IP for ~1&ndash;2 minutes while the
          LoadBalancer reconverges.
        </span>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            flexWrap: "wrap",
          }}
        >
          <button
            type="button"
            className="glass-button glass-button--primary"
            onClick={() => recreate.mutate()}
            disabled={!canRecreate}
            style={{ fontSize: 12, gap: 6, padding: "6px 16px" }}
            title={
              acrName
                ? "Enqueue a deploy task with confirm_recreate=true"
                : "Select an ACR in the Resources panel to enable the recreate button"
            }
          >
            {buttonIcon}
            {buttonLabel}
          </button>
          {recreate.isSuccess && (
            <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
              Track progress in the OpenAPI Deploy panel above.
            </span>
          )}
          {!acrName && (
            <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
              ACR coordinates missing — set them in the Resources panel first.
            </span>
          )}
          {errMessage && (
            <span style={{ fontSize: 12, color: "var(--danger)" }}>
              Could not enqueue deploy: {errMessage}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
