import { api } from "@/api/client";

export interface AksAgentPool {
  name: string;
  vm_size: string | null;
  count: number | null;
  min_count: number | null;
  max_count: number | null;
  os_type: string | null;
  mode: string | null;
  power_state: string | null;
  enable_auto_scaling: boolean | null;
}

export interface AksClusterSummary {
  name: string;
  resource_group: string;
  region: string;
  k8s_version: string | null;
  provisioning_state: string | null;
  power_state: string | null;
  node_count: number | null;
  node_sku: string | null;
  kubelet_object_id: string | null;
  agent_pools?: AksAgentPool[];
  network_plugin?: string | null;
  fqdn?: string | null;
}

export interface WarmupDbInfo {
  name: string;
  mol_type: string;
  status: "Ready" | "Loading" | "Failed" | "Unknown";
  nodes_ready: number;
  nodes_failed: number;
  nodes_active: number;
  total_jobs: number;
}

export interface WarmupStatus {
  warm: boolean;
  workspace_ready: number;
  workspace_desired: number;
  databases: WarmupDbInfo[];
  vmtouch_ready: number;
  namespaces: string[];
  error?: string;
}

export interface StorageSummary {
  name: string;
  region: string;
  sku: string | null;
  kind: string | null;
  public_network_access: string | null;
  is_hns_enabled: boolean | null;
  containers: {
    name: string;
    public_access: string | null;
    last_modified_time: string | null;
  }[];
}

export interface AcrSummary {
  name: string;
  login_server: string;
  sku: string | null;
  expected_image_tags: Record<string, string>;
  actual_tags?: Record<string, string[]>;
  building_images?: string[];
  build_details?: { image: string; status: string; run_id: string }[];
}

export interface VmStatus {
  name: string;
  region: string;
  vm_size: string | null;
  provisioning_state: string | null;
  power_state: string | null;
  os_disk_gb: number | null;
  public_ip: string | null;
  fqdn: string | null;
  has_managed_identity: boolean;
  identity_type: string | null;
}

export interface K8sNode {
  name: string;
  status: string;
  roles: string;
  age: string;
  version: string;
  internal_ip: string;
  os_image: string;
  kernel: string;
  runtime: string;
}

export interface K8sPod {
  namespace: string;
  name: string;
  ready: string;
  status: string;
  restarts: number;
  age: string;
  node: string;
}

export interface K8sNodeMetrics {
  name: string;
  cpu: string;
  cpu_pct: number;
  memory: string;
  memory_pct: number;
  memory_total: string;
  /** Raw millicores currently in use (matches `cpu` numerically). */
  cpu_m?: number;
  /** Raw KiB currently in use (matches `memory` numerically). */
  mem_ki?: number;
  /** Node capacity in millicores. */
  cpu_capacity_m?: number;
  /** Node capacity in KiB. */
  mem_capacity_ki?: number;
  /** AKS agent pool name (`agentpool` label). Empty when missing. */
  pool?: string;
  /** True when the node's `Ready` condition is `True`. */
  ready?: boolean;
  /** Map of K8s node conditions ({Ready: True, MemoryPressure: False, ...}). */
  conditions?: Record<string, string>;
}

export const monitoringApi = {
  aks: (subscriptionId: string, rg: string) =>
    api.get<{ clusters: AksClusterSummary[] }>(
      `/monitor/aks?subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(rg)}`,
    ),

  storage: (subscriptionId: string, rg: string, accountName: string) =>
    api.get<StorageSummary>(
      `/monitor/storage?subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(rg)}&account_name=${encodeURIComponent(accountName)}`,
    ),

  acr: (subscriptionId: string, rg: string, registryName: string) =>
    api.get<AcrSummary>(
      `/monitor/acr?subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(rg)}&registry_name=${encodeURIComponent(registryName)}`,
    ),

  terminal: (subscriptionId: string, rg: string, vmName: string) =>
    api.get<VmStatus>(
      `/monitor/terminal?subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(rg)}&vm_name=${encodeURIComponent(vmName)}`,
    ),

  runAksCommand: (
    subscriptionId: string,
    rg: string,
    clusterName: string,
    command: string,
  ) =>
    api.post<{
      exit_code: number;
      output: string;
      started_at: string | null;
      finished_at: string | null;
    }>("/monitor/aks/run-command", {
      subscription_id: subscriptionId,
      resource_group: rg,
      cluster_name: clusterName,
      command,
    }),

  k8sNodes: (subscriptionId: string, rg: string, clusterName: string) =>
    api.get<{ nodes: K8sNode[] }>(
      `/monitor/aks/nodes?subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(rg)}&cluster_name=${encodeURIComponent(clusterName)}`,
    ),

  k8sPods: (subscriptionId: string, rg: string, clusterName: string) =>
    api.get<{ pods: K8sPod[] }>(
      `/monitor/aks/pods?subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(rg)}&cluster_name=${encodeURIComponent(clusterName)}`,
    ),

  k8sTopNodes: (subscriptionId: string, rg: string, clusterName: string) =>
    api.get<{ nodes: K8sNodeMetrics[] }>(
      `/monitor/aks/top-nodes?subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(rg)}&cluster_name=${encodeURIComponent(clusterName)}`,
    ),

  k8sPodLogs: (
    subscriptionId: string,
    rg: string,
    clusterName: string,
    namespace: string,
    podName: string,
    tail?: number,
  ) =>
    api.get<{ logs: string; pod_name: string; namespace: string }>(
      `/monitor/aks/pod-logs?subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(rg)}&cluster_name=${encodeURIComponent(clusterName)}&namespace=${encodeURIComponent(namespace)}&pod_name=${encodeURIComponent(podName)}&tail=${tail ?? 200}`,
    ),

  buildAcrImages: (
    subscriptionId: string,
    rg: string,
    registryName: string,
    images?: string[],
  ) =>
    api.post<{
      results: {
        image: string;
        status: string;
        run_id?: string;
        error?: string;
        output?: string;
        acr_status?: string;
      }[];
    }>("/acr/build-images", {
      subscription_id: subscriptionId,
      resource_group: rg,
      registry_name: registryName,
      ...(images?.length ? { images } : {}),
    }),

  serviceIp: (
    subscriptionId: string,
    rg: string,
    clusterName: string,
    serviceName: string,
  ) =>
    api.get<{ service_name: string; external_ip: string }>(
      `/monitor/aks/service-ip?subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(rg)}&cluster_name=${encodeURIComponent(clusterName)}&service_name=${encodeURIComponent(serviceName)}`,
    ),

  prepareBlastDb: (
    subscriptionId: string,
    storageRg: string,
    accountName: string,
    dbName: string,
  ) =>
    api.post<{
      ok: boolean;
      db_name: string;
      files_copied?: number;
      files_already_copying?: number;
      files_total?: number;
      source_version?: string;
      output: string;
      async?: boolean;
    }>("/storage/prepare-db", {
      subscription_id: subscriptionId,
      storage_resource_group: storageRg,
      account_name: accountName,
      db_name: dbName,
    }),

  warmupStatus: (subscriptionId: string, rg: string, clusterName: string) =>
    api.get<WarmupStatus>(
      `/monitor/aks/warmup-status?subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(rg)}&cluster_name=${encodeURIComponent(clusterName)}`,
    ),

  startWarmup: (body: {
    subscription_id: string;
    resource_group: string;
    storage_account: string;
    storage_resource_group?: string;
    region?: string;
    db: string;
    db_display_name?: string;
    program?: string;
    aks_cluster_name: string;
    machine_type?: string;
    num_nodes?: number;
    acr_resource_group?: string;
    acr_name?: string;
    terminal_resource_group?: string;
    terminal_vm_name?: string;
  }) => api.post<{ instance_id: string; db: string }>("/warmup/start", body),

  warmupOrchStatus: (instanceId: string) =>
    api.get<{
      instance_id: string;
      runtime_status: string;
      custom_status: {
        phase: string;
        db: string;
        steps?: Record<string, unknown>;
      } | null;
      output: { status: string; db: string; error?: string } | null;
    }>(`/warmup/${encodeURIComponent(instanceId)}/status`),
};