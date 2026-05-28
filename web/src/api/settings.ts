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

export interface VnetPeeringNsgRuleRequest {
  subscription_id: string;
  resource_group: string;
  cluster_name: string;
  target_subscription_id: string;
  target_resource_group: string;
  target_vnet_name: string;
  target_ip?: string;
  /** Allowlist subset of `{80, 443}`. Defaults to both on the backend. */
  ports?: number[];
  /**
   * When true the backend resolves the NSG, runs every permission /
   * collision / priority check, and returns the planned `rule` body
   * (with `applied=false`, `skipped_reason="dry_run"`) without calling
   * `begin_create_or_update`. The SPA uses this for the 2-step confirm
   * UI (preview → commit).
   */
  dry_run?: boolean;
}

export interface VnetPeeringNsgContext {
  target_subnet_id: string;
  target_subnet_name: string;
  target_subnet_prefixes: string[];
  nsg_id: string | null;
  nsg_resource_group: string | null;
  nsg_name: string | null;
  aks_vnet_address_prefixes: string[];
  target_ip: string;
}

export interface VnetPeeringNsgRuleApplied {
  applied: boolean;
  rule_name: string;
  nsg_id: string;
  priority: number | null;
  source_prefixes: string[];
  destination_ip: string;
  ports: number[];
  skipped_reason: string | null;
  conflict_existing: Record<string, unknown> | null;
}

export type VnetPeeringNsgSkipReason =
  | "target_ip_not_in_any_subnet"
  | "no_nsg_attached"
  | "permission_denied"
  | "already_present"
  | "name_collision"
  | "no_free_priority"
  | "dry_run"
  | null;

export interface VnetPeeringNsgRuleResponse {
  applied: boolean;
  skipped_reason: VnetPeeringNsgSkipReason | string | null;
  rule?: VnetPeeringNsgRuleApplied;
  nsg_context?: VnetPeeringNsgContext;
  /** Populated when `skipped_reason === "permission_denied"`. */
  cli_hint?: string;
  /** Populated when `skipped_reason === "target_ip_not_in_any_subnet"`. */
  aks_vnet_id?: string;
  target_vnet_id?: string;
  target_ip?: string;
  /** Deterministic rule name the backend would write (echoed even on dry-run). */
  planned_rule_name?: string;
  /** Echoes the request flag so the SPA can tell preview from commit response. */
  dry_run?: boolean;
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

  /** Explicit follow-up action when the probe is unreachable: write an
   *  inbound-allow rule on the target subnet's NSG (source = AKS VNet
   *  CIDR, destination = target_ip/32, ports ⊆ {80, 443}). The backend
   *  derives all sensitive parameters from the resolved VNet pair — the
   *  caller never supplies CIDRs or wildcards. */
  applyPeeringNsgRule: (body: VnetPeeringNsgRuleRequest) =>
    api.post<VnetPeeringNsgRuleResponse>(
      "/settings/vnet-peering/apply-nsg-rule",
      body,
    ),
};
