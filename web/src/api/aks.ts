import { api } from "@/api/client";
import type { OrchestrationStatus } from "@/api/shared";
import type { AutoWarmupPreference } from "@/api/monitoring";

export interface AksProvisionRequest {
  subscription_id: string;
  resource_group: string;
  region: string;
  cluster_name: string;
  node_sku?: string;
  node_count?: number;
  /** AKS system pool VM size. Mirrors sibling repo
   *  constants.py::ELB_DFLT_AZURE_SYSTEM_VM_SIZE (Standard_D2s_v3). */
  system_vm_size?: string;
  /** Node count for the system pool (default 1, capped at 3). */
  system_node_count?: number;
  acr_resource_group?: string;
  acr_name?: string;
  storage_resource_group?: string;
  storage_account?: string;
  /** Free-form cluster classification ("heavy" / "light" / "gpu" /
   *  "general" / ""). Written to ARM as the `elb-tier` tag so the
   *  dashboard can group multi-cluster deployments. Empty string =
   *  do not write the tag. */
  tier?: string;
}

export interface AksProvisionResponse {
  /** Celery task id — poll /api/tasks/{task_id} for live status. The api
   *  route already returns this; if it's missing the FE falls back to
   *  cluster-list polling only and can't detect task failures. */
  task_id?: string;
  /** Mirror of task_id kept for backwards compat with the Functions-era
   *  Durable Functions response shape. */
  id?: string;
  job_id?: string;
  instance_id?: string;
  statusQueryGetUri?: string;
  cluster_name: string;
  resource_group: string;
  region: string;
  node_sku: string;
  node_count: number;
  system_vm_size?: string;
  system_node_count?: number;
  status: string;
  message: string;
  roles_assigned?: string[];
}

export interface AksSku {
  name: string;
  vCPUs: number;
  memoryGiB: number;
  category: string;
  series: string;
  hourlyUsd: number;
  /** "system" — only suitable for the systempool;
   *  "blast"  — only suitable for the workload pool;
   *  "both"   — fits either. */
  role: "system" | "blast" | "both";
  /** Stable group id used for <optgroup> rendering. */
  group: string;
}

export interface AksSkuListResponse {
  skus: AksSku[];
  default: string;
  default_sku: string;
  /** Default SKU for the small AKS system pool. */
  default_system_sku: string;
  /** Map of group id → human-friendly label (used as `<optgroup label>`). */
  group_labels?: Record<string, string>;
  /** Stable display order for the group ids in the dropdown. */
  group_order?: string[];
  degraded?: boolean;
  degraded_reason?: string;
}

export interface OpenApiTokenStatus {
  configured: boolean;
  token: string;
  masked_token: string;
  header_name: string;
  env_name: string;
  source: string;
  updated_at?: string | null;
  generated?: boolean;
  rotated?: boolean;
}

export interface OpenApiDeploymentStatus {
  configured: boolean;
  deployment_name: string;
  container_name: string;
  namespace: string;
  image: string;
  image_repository: string;
  image_tag: string;
}

export interface AksAvailableSkusResponse {
  region: string;
  /** SKU names that the subscription can actually deploy in this region.
   *  The dropdown should treat anything missing from this list as
   *  ineligible regardless of whether it appears in the allow-list. */
  available: string[];
  /** Per-SKU reason rows for everything in the allow-list that is
   *  *not* deployable here. Used by the dropdown to render a tooltip
   *  with the Azure restriction code (e.g. NotAvailableForSubscription). */
  unavailable: {
    name: string;
    available: false;
    reason: string | null;
    location_restricted?: boolean;
  }[];
  /** True when the Azure listing failed entirely — the SPA should not
   *  filter the dropdown in that case (better to allow + hit the live
   *  BadRequest than to silently hide everything). */
  degraded: boolean;
}

export interface AksPreflightRequest {
  subscription_id: string;
  resource_group: string;
  region: string;
  cluster_name: string;
  node_sku: string;
  node_count: number;
  system_vm_size: string;
  system_node_count: number;
}

export interface AksPreflightCheck {
  /** Stable id — `region` | `skus` | `quota` | `resource_group`. */
  name: string;
  /** `ok` | `warn` | `fail`. The modal renders these with green / amber /
   *  red icons respectively. `fail` blocks submit; `warn` does not. */
  status: "ok" | "warn" | "fail";
  message: string;
  details?: Record<string, unknown>;
}

export interface AksPreflightResponse {
  /** False whenever at least one `checks[]` row has `status="fail"`. */
  ok: boolean;
  checks: AksPreflightCheck[];
  /** Azure portal deep link to the cluster's overview blade. Surfaced
   *  in the banner once provisioning starts and the resource is
   *  visible. */
  portal_url: string | null;
}

export interface AksLifecycleResponse {
  cluster_name?: string;
  task_id?: string;
  status: string;
}

export interface AksCancelProvisionResponse {
  task_id: string;
  job_id: string | null;
  /** Celery status the task was in when the request arrived. */
  previous_status: string;
  /** True when the task was still running (PENDING/RECEIVED/STARTED/RETRY)
   *  and the revoke signal was actually sent. False on a noop cancel of
   *  an already-terminal task. */
  was_running: boolean;
  cancelled: boolean;
  /** Approximate wait before the worker honors SIGTERM and the dashboard
   *  banner clears. Surfaced in the toast so the user understands the
   *  brief delay. */
  settle_after_seconds: number;
}

export interface AksRecentFailedProvision {
  job_id: string;
  task_id: string | null;
  status: string;
  phase: string | null;
  /** Raw Azure / Celery error string, truncated to 500 chars by the
   *  task. Feeds the dashboard's `armErrorClassifier` so the banner
   *  can render a friendly headline + portal link. */
  error_code: string | null;
  cluster_name: string | null;
  region: string | null;
  resource_group: string | null;
  subscription_id: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface AksRecentFailedProvisionsResponse {
  jobs: AksRecentFailedProvision[];
  /** True when the state-repo query failed entirely. The FE should
   *  fall back to the localStorage source rather than treat an empty
   *  list as "no failures". */
  degraded: boolean;
}

export const aksApi = {
  listSkus: () => api.get<AksSkuListResponse>("/aks/skus"),

  availableSkus: (subscriptionId: string, region: string) =>
    api.get<AksAvailableSkusResponse>(
      `/aks/available-skus?subscription_id=${encodeURIComponent(subscriptionId)}&region=${encodeURIComponent(region)}`,
    ),

  preflight: (req: AksPreflightRequest) =>
    api.post<AksPreflightResponse>("/aks/preflight", req),

  provision: (req: AksProvisionRequest) =>
    api.post<AksProvisionResponse>("/aks/provision", req),

  cancelProvision: (taskId: string) =>
    api.post<AksCancelProvisionResponse>(
      `/aks/cancel-provision/${encodeURIComponent(taskId)}`,
      {},
    ),

  recentFailedProvisions: (hours: number = 24, limit: number = 10) =>
    api.get<AksRecentFailedProvisionsResponse>(
      `/aks/recent-failed-provisions?hours=${hours}&limit=${limit}`,
    ),

  delete: (subscriptionId: string, rg: string, clusterName: string) =>
    api.post<AksLifecycleResponse>("/aks/delete", {
      subscription_id: subscriptionId,
      resource_group: rg,
      cluster_name: clusterName,
    }),

  start: (
    subscriptionId: string,
    rg: string,
    clusterName: string,
    autoWarmup?: Partial<AutoWarmupPreference>,
  ) =>
    api.post<AksLifecycleResponse>("/aks/start", {
      subscription_id: subscriptionId,
      resource_group: rg,
      cluster_name: clusterName,
      ...(autoWarmup ? { auto_warmup: autoWarmup } : {}),
    }),

  stop: (subscriptionId: string, rg: string, clusterName: string) =>
    api.post<AksLifecycleResponse>("/aks/stop", {
      subscription_id: subscriptionId,
      resource_group: rg,
      cluster_name: clusterName,
    }),

  assignRoles: (
    subscriptionId: string,
    rg: string,
    clusterName: string,
    acrRg: string,
    acrName: string,
    storageRg: string,
    storageAccount: string,
  ) =>
    api.post<{ kubelet_oid: string; roles_assigned: string[] }>(
      `/aks/${encodeURIComponent(clusterName)}/assign-roles`,
      {
        subscription_id: subscriptionId,
        resource_group: rg,
        acr_resource_group: acrRg,
        acr_name: acrName,
        storage_resource_group: storageRg,
        storage_account: storageAccount,
      },
    ),

  deployOpenApi: (
    subscriptionId: string,
    rg: string,
    clusterName: string,
    acrName?: string,
    storageAccount?: string,
  ) =>
    api.post<{ id: string; statusQueryGetUri?: string }>("/aks/openapi/deploy", {
      subscription_id: subscriptionId,
      resource_group: rg,
      cluster_name: clusterName,
      acr_name: acrName,
      storage_account: storageAccount,
    }),

  openApiDeployStatus: (instanceId: string) =>
    api.get<
      OrchestrationStatus<{
        cluster_name?: string;
        resource_group?: string;
        status?: string;
        openapi_deploy?: { error?: string };
        workload_identity?: { error?: string };
      }>
    >(`/aks/openapi/deploy/${encodeURIComponent(instanceId)}/status`),

  proxyOpenApiSpec: (subscriptionId: string, rg: string, clusterName: string) =>
    api.get<Record<string, unknown>>(
      `/aks/openapi/spec?subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(rg)}&cluster_name=${encodeURIComponent(clusterName)}`,
    ),

  openApiDeployment: (subscriptionId: string, rg: string, clusterName: string) =>
    api.get<OpenApiDeploymentStatus>(
      `/aks/openapi/deployment?subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(rg)}&cluster_name=${encodeURIComponent(clusterName)}`,
    ),

  openApiToken: (subscriptionId: string, rg: string, clusterName: string) =>
    api.get<OpenApiTokenStatus>(
      `/aks/openapi/token?subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(rg)}&cluster_name=${encodeURIComponent(clusterName)}`,
    ),

  generateOpenApiToken: (
    subscriptionId: string,
    rg: string,
    clusterName: string,
    regenerate: boolean,
  ) =>
    api.post<OpenApiTokenStatus>("/aks/openapi/token", {
      subscription_id: subscriptionId,
      resource_group: rg,
      cluster_name: clusterName,
      regenerate,
    }),
};
