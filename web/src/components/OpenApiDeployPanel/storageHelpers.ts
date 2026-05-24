export const FINISHED_STATUSES = new Set(["Completed", "Failed", "Terminated"]);

export const DEPLOY_DISCOVERY_TIMEOUT_MS = 180_000;

// If the deploy task stays in "Pending" (Celery has no record of it ever
// starting) for this long, assume the task was lost — typically because
// the api enqueued it to a broker the worker is not consuming from, or
// the task id was wiped from the result backend. Surface a clear error
// instead of trapping the panel in a permanent "Deploying..." state.
export const STALE_PENDING_TIMEOUT_MS = 300_000;

export function deployStorageKey(
  subscriptionId: string,
  resourceGroup: string,
  clusterName: string,
): string {
  return `elb-openapi-deploy-${subscriptionId}-${resourceGroup}-${clusterName}`;
}

export interface StoredDeploy {
  instanceId: string;
  startedAt: number;
}

export function readStoredDeploy(key: string): StoredDeploy | null {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as {
      instanceId?: string;
      startedAt?: number;
    };
    if (!parsed.instanceId || !parsed.startedAt) return null;
    return { instanceId: parsed.instanceId, startedAt: parsed.startedAt };
  } catch {
    return null;
  }
}

export function writeStoredDeploy(
  key: string,
  instanceId: string,
  startedAt: number,
): void {
  try {
    localStorage.setItem(key, JSON.stringify({ instanceId, startedAt }));
  } catch {
    /* best-effort */
  }
}

export function clearStoredDeploy(key: string): void {
  try {
    localStorage.removeItem(key);
  } catch {
    /* best-effort */
  }
}

export function formatDeployPhase(phase?: string): string {
  switch (phase) {
    case "setup_workload_identity":
      return "Setting up workload identity";
    case "applying_manifests":
      return "Applying OpenAPI manifests";
    case "waiting_for_external_ip":
      return "Waiting for OpenAPI LoadBalancer IP";
    case "waiting_for_ready_replicas":
      return "Waiting for OpenAPI pod readiness";
    default:
      return phase ?? "Deploying OpenAPI";
  }
}
