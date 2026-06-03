/**
 * Pure resolver for the Azure scope (subscription, storage account, resource
 * group, cluster) a BLAST Results page should target.
 *
 * Responsibility: derive the four scoping identifiers from the strongest
 * available source, in priority order, so cross-RG multi-cluster fleets are
 * addressed correctly.
 * Edit boundaries: pure function only — no React, no network, no Azure SDK.
 * Key entry points: resolveBlastJobScope.
 * Risky contracts: the AKS cluster picker is subscription-wide, so a job's
 *   cluster may live OUTSIDE the workspace anchor RG. The job's own
 *   `infrastructure` block (recorded by the backend) MUST win over the anchor
 *   config; otherwise results listing / download / export / cancel target the
 *   wrong resource group/cluster.
 * Validation: web/src/pages/blastResults/blastJobScope.test.ts.
 */

/** The `job.infrastructure` block the backend records for a BLAST job. */
export interface BlastJobInfrastructure {
  subscription_id?: string;
  resource_group?: string;
  storage_account?: string;
  cluster_name?: string;
}

/** Workspace-anchor defaults loaded from the saved setup config. */
export interface BlastJobAnchorConfig {
  subscriptionId?: string;
  storageAccountName?: string;
  workloadResourceGroup?: string;
}

export interface BlastJobScopeInputs {
  /** URL query params (deep-link overrides win over everything). */
  searchParams: URLSearchParams;
  /** The submit payload echoed back on the job, if present. */
  payload: Record<string, unknown> | undefined;
  /** The backend-recorded infrastructure block, if present. */
  infrastructure: BlastJobInfrastructure | undefined;
  /** Workspace anchor config (last resort for legacy jobs). */
  config: BlastJobAnchorConfig | null | undefined;
}

export interface BlastJobScope {
  subscriptionId: string;
  storageAccount: string;
  resourceGroup: string;
  clusterName: string;
}

function stringFromPayload(payload: Record<string, unknown> | undefined, key: string): string {
  const value = payload?.[key];
  return typeof value === "string" ? value : "";
}

/**
 * Resolve the four scoping identifiers in priority order:
 *   1. URL query params (explicit deep-link override)
 *   2. submit payload (what the user submitted)
 *   3. job.infrastructure (authoritative — where the job actually ran)
 *   4. workspace anchor config (legacy fallback only)
 *
 * Step 3 is the multi-cluster fix: when the payload omits a field, we must use
 * the job's own infrastructure RG/cluster, NOT the workspace anchor RG, so
 * cross-RG clusters keep working.
 *
 * `clusterName` intentionally has NO last-resort default: an unknown cluster
 * must stay empty rather than silently resolve to the workspace anchor
 * cluster. A wrong cluster guess made cancel target a non-existent AKS
 * resource (the old `"elb-cluster"` fallback) and fail with
 * `cancel_unavailable`; OpenAPI-sibling jobs are now cancelled via the
 * sibling, which owns its own cluster, so the dashboard never needs to guess.
 */
export function resolveBlastJobScope({
  searchParams,
  payload,
  infrastructure,
  config,
}: BlastJobScopeInputs): BlastJobScope {
  const payloadClusterName =
    stringFromPayload(payload, "cluster_name") ||
    stringFromPayload(payload, "aks_cluster_name");

  const subscriptionId =
    searchParams.get("subscription_id") ||
    stringFromPayload(payload, "subscription_id") ||
    infrastructure?.subscription_id ||
    config?.subscriptionId ||
    "";
  const storageAccount =
    searchParams.get("storage_account") ||
    stringFromPayload(payload, "storage_account") ||
    infrastructure?.storage_account ||
    config?.storageAccountName ||
    "";
  const resourceGroup =
    searchParams.get("resource_group") ||
    stringFromPayload(payload, "resource_group") ||
    infrastructure?.resource_group ||
    config?.workloadResourceGroup ||
    "";
  const clusterName =
    searchParams.get("cluster_name") ||
    payloadClusterName ||
    infrastructure?.cluster_name ||
    "";

  return { subscriptionId, storageAccount, resourceGroup, clusterName };
}
