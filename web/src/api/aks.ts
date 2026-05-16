import { api } from "@/api/client";
import type { OrchestrationStatus } from "@/api/shared";

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
}

export interface AksProvisionResponse {
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

export const aksApi = {
  listSkus: () => api.get<AksSkuListResponse>("/aks/skus"),

  provision: (req: AksProvisionRequest) =>
    api.post<AksProvisionResponse>("/aks/provision", req),

  delete: (subscriptionId: string, rg: string, clusterName: string) =>
    api.post<{ cluster_name: string; status: string }>("/aks/delete", {
      subscription_id: subscriptionId,
      resource_group: rg,
      cluster_name: clusterName,
    }),

  start: (subscriptionId: string, rg: string, clusterName: string) =>
    api.post<{ cluster_name: string; status: string }>("/aks/start", {
      subscription_id: subscriptionId,
      resource_group: rg,
      cluster_name: clusterName,
    }),

  stop: (subscriptionId: string, rg: string, clusterName: string) =>
    api.post<{ cluster_name: string; status: string }>("/aks/stop", {
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
};