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

/** Warm-cache persistence mode for an AKS cluster's BLAST DB staging.
 *  - `ephemeral`: redownload + vmtouch on every start (current behaviour).
 *  - `node_disk`: persist the staged DB on the node OS/managed disk.
 *  - `data_disk`: persist on a dedicated managed data disk (PVC). */
export type WarmCacheMode = "ephemeral" | "node_disk" | "data_disk";

export interface PerformancePreference {
  subscription_id: string;
  resource_group: string;
  cluster_name: string;
  warm_cache_mode: WarmCacheMode;
  updated_at: string;
  owner_oid: string;
  tenant_id: string;
}

export interface PerformancePreferenceResponse {
  /** Null when no preference row exists yet (effective mode is the default). */
  preference: PerformancePreference | null;
  warm_cache_mode: WarmCacheMode;
}

export interface PerformancePreferenceQuery {
  subscription_id: string;
  resource_group: string;
  cluster_name: string;
}

export interface PerformancePreferencePutRequest {
  subscription_id: string;
  resource_group: string;
  cluster_name: string;
  warm_cache_mode: WarmCacheMode;
}

export interface PerformancePreferenceSavedResponse {
  status: "saved";
  preference: PerformancePreference;
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
  /**
   * The elb-openapi internal-LB IP. Auto-detected per cluster by the UI (the
   * internal-LB IP differs per cluster topology). When omitted, the backend
   * falls back to a legacy default that is correct only for auto-VNet clusters.
   */
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
  /**
   * Present only when a target-VNet peering failed with an Azure RBAC denial.
   * Carries the exact least-privilege `az role assignment create` the operator
   * can paste to grant the dashboard managed identity `Network Contributor` on
   * the target VNet. Unlike `recovery_command`, this fixes target-to-AKS
   * peering (not platform-to-AKS).
   */
  rbac_remediation?: {
    role: string;
    scope: string;
    command: string;
    message: string;
  };
  /** Helper-level partial-failure or skip explanation. */
  error?: string;
  skipped?: boolean;
  reason?: string;
  /** Human-readable elaboration on a skip (e.g. BYO-subnet self-peering). */
  message?: string;
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

export interface VnetPeeringRemoteVnet {
  id: string;
  name: string;
  resource_group: string;
  subscription_id: string;
}

export interface VnetPeeringExistingItem {
  name: string;
  /** Connected / Initiated / Disconnected / Unknown. */
  peering_state: string;
  /** Succeeded / Updating / Failed / Unknown. */
  provisioning_state: string;
  remote_vnet: VnetPeeringRemoteVnet | null;
  /**
   * Tri-state orphan signal. `true` = the remote VNet still exists;
   * `false` = the remote VNet was deleted, so this peering is a stale ghost
   * the operator should remove; `null` = not probed / could not be determined
   * (RBAC, cross-tenant, transport fault). The backend only probes peerings in
   * the `Disconnected` state.
   */
  remote_vnet_exists: boolean | null;
  remote_address_prefixes: string[];
  allow_virtual_network_access: boolean;
  allow_forwarded_traffic: boolean;
  allow_gateway_transit: boolean;
  use_remote_gateways: boolean;
}

export interface VnetPeeringExistingResponse {
  /** ARM id of the cluster's AKS VNet whose peerings were listed. */
  aks_vnet: string;
  aks_vnet_name: string;
  node_resource_group: string;
  peerings: VnetPeeringExistingItem[];
  /** True when there is genuinely no AKS VNet to inspect (BYO self-VNet, etc.). */
  skipped: boolean;
  reason: string | null;
  /** Non-null when the listing call itself failed (e.g. RBAC denial). */
  error: string | null;
}

export interface VnetPeeringDeleteRequest {
  subscription_id: string;
  resource_group: string;
  cluster_name: string;
  /** The local (AKS-side) peering name to remove. */
  peering_name: string;
}

export interface VnetPeeringDeleteResponse {
  deleted: boolean;
  skipped: boolean;
  reason: string | null;
  error: string | null;
  peering_name: string;
}

export type ServiceBusAuthMode = "entra" | "sas";

/** Service Bus integration config (no secret material — `sas_secret_name` is a
 *  Key Vault secret NAME, never the connection string). */
export interface ServiceBusConfig {
  enabled: boolean;
  auth_mode: ServiceBusAuthMode;
  namespace_fqdn: string;
  request_queue: string;
  completion_topic: string;
  sas_secret_name: string;
  subscription_id: string;
  resource_group: string;
  cluster_name: string;
  storage_account: string;
  dlq_cleanup_enabled: boolean;
  dlq_max_age_days: number;
  dlq_max_count: number;
  dlq_cleanup_batch: number;
  updated_at: string;
  owner_oid: string;
  tenant_id: string;
}

export interface ServiceBusCounts {
  available: boolean;
  reason?: string;
  queue?: {
    active_message_count: number;
    dead_letter_message_count: number;
    scheduled_message_count: number;
    total_message_count: number;
  } | null;
  dead_letter?: number | null;
  subscriptions?: Array<{
    name: string;
    active_message_count: number;
    dead_letter_message_count: number;
  }>;
}

export interface ServiceBusStatusResponse {
  config: ServiceBusConfig;
  env_enabled: boolean;
  effective_enabled: boolean;
  /**
   * Raw deployment master switch (`SERVICEBUS_ENABLED`), independent of the
   * saved config row. When `config.enabled` is true but this is false, the
   * integration is dormant because the deployment never opted in — the
   * Settings section surfaces this so the operator knows why it is not live.
   */
  env_gate_enabled: boolean;
  counts: ServiceBusCounts;
}

export interface ServiceBusTestResponse {
  reachable: boolean;
  peeked?: number;
  reason?: string;
  detail?: string;
  auth_mode?: ServiceBusAuthMode;
}

export interface ServiceBusDiscoverResponse {
  namespaces?: Array<{ name: string; id: string; location: string; fqdn: string }>;
  namespace_fqdn?: string;
  queues?: string[];
  topics?: string[];
  reason?: string;
}

export interface ServiceBusPurgeResponse {
  status: string;
  dead_letter: boolean;
  removed: number;
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

  /** Read the per-cluster warm-cache mode. Returns the default `ephemeral`
   *  (and `preference: null`) when no row exists — never 404s. */
  getPerformance: (q: PerformancePreferenceQuery) =>
    api.get<PerformancePreferenceResponse>(
      `/settings/performance?${querystring({ ...q })}`,
    ),

  /** Persist the per-cluster warm-cache mode. Applies to the NEXT provisioned
   *  cluster — the OS disk type is fixed at create time. */
  putPerformance: (body: PerformancePreferencePutRequest) =>
    api.put<PerformancePreferenceSavedResponse>(
      "/settings/performance",
      body,
    ),

  /** Peer a target VNet with the AKS auto-VNet and probe the elb-openapi
   *  private IP from the dashboard's api sidecar. Synchronous — the
   *  backend returns the summary payload (peerings + probe) in one shot. */
  peerVnet: (body: VnetPeeringRequest) =>
    api.post<VnetPeeringResponse>("/settings/vnet-peering", body),

  /** List the peerings already present on a cluster's AKS VNet (read-only).
   *  Never throws on a routine Azure fault — the backend folds RBAC denials
   *  and BYO self-VNet skips into the 200 payload's `error` / `skipped`. */
  listExistingPeerings: (
    subscriptionId: string,
    resourceGroup: string,
    clusterName: string,
  ) =>
    api.get<VnetPeeringExistingResponse>(
      `/settings/vnet-peering/existing?${querystring({
        subscription_id: subscriptionId,
        resource_group: resourceGroup,
        cluster_name: clusterName,
      })}`,
    ),

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

  /** Remove a single orphaned ("Disconnected") peering from the cluster's
   *  AKS VNet. Symmetric with `peerVnet` — only the AKS-side peering is
   *  deleted (the remote side is typically already gone). Idempotent: a
   *  missing peering returns `deleted=true`. */
  deletePeering: (body: VnetPeeringDeleteRequest) =>
    api.post<VnetPeeringDeleteResponse>(
      "/settings/vnet-peering/delete",
      body,
    ),

  /** Read the Service Bus integration config + best-effort runtime counts.
   *  Never 404s; returns a disabled default when no row exists. */
  getServiceBus: () => api.get<ServiceBusStatusResponse>("/settings/service-bus"),

  /** Persist the Service Bus integration config (validated server-side). */
  putServiceBus: (body: Partial<ServiceBusConfig>) =>
    api.put<{ status: string; config: ServiceBusConfig }>("/settings/service-bus", body),

  /** Non-destructive reachability probe (peeks the request queue). */
  testServiceBus: (body: Partial<ServiceBusConfig>) =>
    api.post<ServiceBusTestResponse>("/settings/service-bus/test", body),

  /** Discover namespaces (pass subscription_id) or queues/topics (pass
   *  namespace_fqdn) for the Settings dropdowns. */
  discoverServiceBus: (body: { subscription_id?: string; namespace_fqdn?: string; auth_mode?: ServiceBusAuthMode; sas_secret_name?: string }) =>
    api.post<ServiceBusDiscoverResponse>("/settings/service-bus/discover", body),

  /** Manual purge of the main queue or its DLQ (operator action). */
  purgeServiceBus: (body: { dead_letter: boolean; max_messages?: number }) =>
    api.post<ServiceBusPurgeResponse>("/settings/service-bus/purge", body),
};
