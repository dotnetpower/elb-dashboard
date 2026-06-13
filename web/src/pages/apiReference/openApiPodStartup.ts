// Discriminator for the `elb-openapi` spec route's "pod still starting"
// degraded payload. Kept in its own module (like `isPeerWithPlatformRecovery`)
// so the API Reference page and its tests share one definition.
//
// The spec route (`GET /api/aks/openapi/spec`) returns a 200 placeholder with a
// `degraded_reason` when it cannot fetch the live spec. Two reasons mean the
// pod simply is not serving yet — `openapi_pod_starting` (image cold-pull on a
// fresh node, self-resolving) and `openapi_pod_not_ready` (up but failing
// readiness, e.g. CrashLoopBackOff). Neither is a VNet-peering break, so they
// must render a calm state instead of the red "Repair VNet peering" error.

export interface OpenApiSpecDegraded {
  degraded_reason?: string;
  pod_state?: string;
  pod_reason?: string;
  pod_message?: string;
}

/** Return the degraded payload when it represents a still-starting / not-ready
 *  pod, otherwise null. Accepts unknown to keep call sites unfussy. */
export function readOpenApiPodStartup(payload: unknown): OpenApiSpecDegraded | null {
  if (!payload || typeof payload !== "object") return null;
  const rec = payload as OpenApiSpecDegraded;
  if (
    rec.degraded_reason === "openapi_pod_starting" ||
    rec.degraded_reason === "openapi_pod_not_ready"
  ) {
    return rec;
  }
  return null;
}
