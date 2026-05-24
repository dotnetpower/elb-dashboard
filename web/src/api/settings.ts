/**
 * settings — typed clients for `/api/settings/*` endpoints.
 *
 * Used by the SettingsPanel and `useAppInsights` to:
 *   - read the deployment-injected App Insights connection string,
 *   - look up or provision an Application Insights component,
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

  getAksObservabilityStatus: (q: AksObservabilityStatusQuery) =>
    api.get<AksObservabilityStatus>(
      `/settings/aks-observability?${querystring(q as unknown as Record<string, string>)}`,
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
};
