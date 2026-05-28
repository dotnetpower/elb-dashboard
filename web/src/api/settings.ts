/**
 * settings — typed clients for `/api/settings/*` endpoints.
 *
 * Used by the SettingsPanel and `useAppInsights` to:
 *   - read the deployment-injected App Insights connection string,
 *   - look up or provision an Application Insights component,
 *   - apply a connection string to the server sidecars,
 *   - read AKS Container Insights state and enable it.
 *
 * Provision/enable endpoints return Celery task ids. Poll status via the
 * existing `tasksApi.status` client in `@/api/tasks`.
 */
import { api } from "@/api/client";

export interface AppInsightsStatus {
  deployment_connection_string: string;
  deployment_configured: boolean;
}

export interface AppInsightsComponent {
  id: string;
  name: string;
  location?: string;
  kind?: string;
  application_id?: string;
  instrumentation_key?: string;
  connection_string: string;
  workspace_resource_id?: string;
  provisioning_state?: string;
}

export interface AppInsightsLookupRequest {
  subscription_id: string;
  resource_group?: string;
  component_name: string;
}

export interface AppInsightsProvisionRequest {
  subscription_id: string;
  resource_group: string;
  component_name: string;
  region: string;
  workspace_name: string;
  workspace_resource_group?: string;
  /** Log Analytics retention in days. Backend allows 7-730 from a discrete list. */
  retention_days?: number;
}

export interface AppInsightsApplyRequest {
  connection_string: string;
}

export interface AppInsightsTaskQueuedResponse {
  task_id: string;
  status: "queued";
  statusQueryGetUri: string;
}

export interface AksObservabilityStatus {
  enabled: boolean;
  workspace_resource_id: string | null;
  cluster_provisioning_state: string | null;
}

export interface AksObservabilityEnableRequest {
  subscription_id: string;
  resource_group: string;
  cluster_name: string;
  workspace_resource_id: string;
}

export interface AksObservabilityDisableRequest {
  subscription_id: string;
  resource_group: string;
  cluster_name: string;
}

export interface AksObservabilityStatusQuery {
  subscription_id: string;
  resource_group: string;
  cluster_name: string;
}

export interface VnetPeeringRequest {
  /** Subscription that hosts the AKS cluster (i.e. the dashboard subscription). */
  subscription_id: string;
  /** Resource group of the AKS cluster (NOT the auto MC_* node RG). */
  resource_group: string;
  cluster_name: string;
  /** Subscription that owns the remote VNet whose VMs need to reach `target_ip`. */
  target_subscription_id: string;
  target_resource_group: string;
  target_vnet_name: string;
  /** Optional override. Defaults to the elb-openapi internal-LB IP `10.224.0.7`. */
  target_ip?: string;
  /** Optional path component of the probe URL. Defaults to `/openapi.json`. */
  target_path?: string;
}

export interface VnetPeeringDirection {
  direction: string;
  name: string;
  state: string;
}

export interface VnetPeeringProbe {
  target_ip: string;
  target_path: string;
  url: string;
  reachable: boolean;
  status_code: number | null;
  latency_ms: number;
  message: string;
}

export interface VnetPeeringResponse {
  target_subscription_id?: string;
  target_resource_group?: string;
  target_vnet_name?: string;
  target_vnet?: string;
  aks_vnet?: string;
  node_resource_group?: string;
  peerings?: VnetPeeringDirection[];
  probe?: VnetPeeringProbe;
  recovery_command?: string;
  /** Helper-level partial-failure or skip explanation. */
  error?: string;
  skipped?: boolean;
  reason?: string;
}

function querystring(params: Record<string, string>): string {
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) usp.set(k, v);
  return usp.toString();
}

export const settingsApi = {
  getAppInsightsStatus: () => api.get<AppInsightsStatus>("/settings/app-insights"),

  lookupAppInsights: (body: AppInsightsLookupRequest) =>
    api.post<{ component: AppInsightsComponent }>(
      "/settings/app-insights/lookup",
      body,
    ),

  provisionAppInsights: (body: AppInsightsProvisionRequest) =>
    api.post<AppInsightsTaskQueuedResponse>(
      "/settings/app-insights/provision",
      body,
    ),

  applyAppInsightsToDeployment: (body: AppInsightsApplyRequest) =>
    api.post<AppInsightsTaskQueuedResponse>(
      "/settings/app-insights/apply",
      body,
    ),

  clearAppInsightsFromDeployment: () =>
    api.post<AppInsightsTaskQueuedResponse>(
      "/settings/app-insights/clear",
      {},
    ),

  getAksObservabilityStatus: (q: AksObservabilityStatusQuery) =>
    api.get<AksObservabilityStatus>(
      `/settings/aks-observability?${querystring({ ...q })}`,
    ),

  enableAksObservability: (body: AksObservabilityEnableRequest) =>
    api.post<AppInsightsTaskQueuedResponse>(
      "/settings/aks-observability/enable",
      body,
    ),

  disableAksObservability: (body: AksObservabilityDisableRequest) =>
    api.post<AppInsightsTaskQueuedResponse>(
      "/settings/aks-observability/disable",
      body,
    ),

  /** Peer a target VNet with the AKS auto-VNet and probe the elb-openapi
   *  private IP from the dashboard's api sidecar. Synchronous — the
   *  backend returns the summary payload (peerings + probe) in one shot. */
  peerVnet: (body: VnetPeeringRequest) =>
    api.post<VnetPeeringResponse>("/settings/vnet-peering", body),
};
