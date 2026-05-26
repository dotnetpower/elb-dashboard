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
  /** Raw ARM tag map (empty when the cluster has no tags). */
  tags?: Record<string, string>;
  /** Convenience accessor for the `elb-tier` ARM tag. `null` when unset. */
  tier?: string | null;
  /** True when the cluster carries the ElasticBLAST identification surface
   *  (`managedBy=elb-dashboard` / `app=elastic-blast` tag, or the legacy
   *  `blastpool` + `workload=blast` taint shape). Subscription-wide list
   *  responses filter to managed clusters by default. */
  managed_by_elb?: boolean;
}

export interface WarmupDbInfo {
  name: string;
  mol_type: string;
  status:
    | "Ready"
    | "Loading"
    | "Failed"
    | "Unknown"
    | "Partial"
    | "Released"
    | "Blocked"
    | "Pressure"
    | "Stale";
  nodes_ready: number;
  nodes_failed: number;
  nodes_active: number;
  total_jobs: number;
  shards?: string[];
  progress_pct?: number;
  started_at?: string;
  elapsed_seconds?: number;
  estimated_remaining_seconds?: number;
  active_phase?:
    | "waiting"
    | "starting"
    | "copying_files"
    | "verifying_db"
    | "touching_memory"
    | "completed"
    | "failed"
    | "unknown";
  active_phase_label?: string;
  active_message?: string;
  active_last_log?: string;
  phase_counts?: Record<string, number>;
  pod_statuses?: WarmupPodStatus[];
  source_version?: string;
  source_versions?: string[];
}

export interface WarmupPodStatus {
  pod: string;
  shard: string;
  node: string;
  phase: string;
  phase_label: string;
  message: string;
  last_log?: string;
  started_at?: string;
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

export interface AutoWarmupPreference {
  subscription_id: string;
  resource_group: string;
  cluster_name: string;
  storage_account: string;
  storage_resource_group: string;
  region?: string;
  databases: string[];
  programs?: Record<string, string>;
  enabled: boolean;
  acr_resource_group?: string;
  acr_name?: string;
  terminal_resource_group?: string;
  terminal_vm_name?: string;
  machine_type?: string;
  num_nodes?: number;
  last_ready?: boolean;
  last_triggered_at?: string;
  updated_at?: string;
}

export interface StorageSummary {
  name: string;
  region: string;
  sku: string | null;
  kind: string | null;
  public_network_access: string | null;
  is_hns_enabled: boolean | null;
  /** Backend graceful-degrade flag — set when ARM returned 401/403/404 etc. */
  degraded?: boolean;
  /** Stable degraded reason code (see `web/src/utils/monitorDegraded.ts`). */
  degraded_reason?: string;
  containers: {
    name: string;
    public_access: string | null;
    last_modified_time: string | null;
    blob_count?: number | null;
    size_bytes?: number | null;
    usage_pending?: boolean;
    usage_truncated?: boolean;
    usage_error?: string | null;
    usage_cache_state?: string | null;
    usage_refreshed_at?: string | null;
  }[];
  containers_usage_cache?: {
    state: string;
    hit: boolean;
    pending: boolean;
    age_seconds: number | null;
    refreshed_at: string | null;
  };
}

export interface AcrSummary {
  name: string;
  login_server: string;
  sku: string | null;
  expected_image_tags: Record<string, string>;
  actual_tags?: Record<string, string[]>;
  building_images?: string[];
  build_details?: { image: string; status: string; run_id: string }[];
  /** Backend graceful-degrade flag — set when ARM returned 401/403/404 etc. */
  degraded?: boolean;
  /** Stable degraded reason code (see `web/src/utils/monitorDegraded.ts`). */
  degraded_reason?: string;
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
  /**
   * List AKS clusters. When `rg` is omitted (or empty), the backend returns
   * every ElasticBLAST-managed cluster in the subscription — filtered by ARM
   * tag `managedBy=elb-dashboard` / `app=elastic-blast` with a `blastpool`
   * legacy fallback, so foreign workloads in the same subscription are not
   * pulled in. Pass `rg` explicitly to constrain to a single resource group
   * (the legacy RG-scoped behaviour; no tag filter).
   */
  aks: (subscriptionId: string, rg?: string) => {
    const params = new URLSearchParams({ subscription_id: subscriptionId });
    if (rg) params.set("resource_group", rg);
    return api.get<{
      clusters: AksClusterSummary[];
      scope?: "subscription";
      degraded?: boolean;
      degraded_reason?: string;
    }>(`/monitor/aks?${params.toString()}`);
  },

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

  k8sPodDescribe: (
    subscriptionId: string,
    rg: string,
    clusterName: string,
    namespace: string,
    podName: string,
  ) =>
    api.get<{ describe: string }>(
      `/monitor/aks/pod-describe?subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(rg)}&cluster_name=${encodeURIComponent(clusterName)}&namespace=${encodeURIComponent(namespace)}&pod_name=${encodeURIComponent(podName)}`,
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
    api.get<{
      service_name: string;
      external_ip: string | null;
      available: boolean;
      status: "ready" | "missing_or_pending";
    }>(
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

  /**
   * Abort an in-flight prepare-db copy. Calls ``abort_copy`` against every
   * pending blob and rewrites metadata with ``copy_status.phase=cancelled``
   * so the SPA flips back to a clean state without waiting the 2 h
   * stale-recovery window.
   */
  cancelPrepareBlastDb: (
    subscriptionId: string,
    storageRg: string,
    accountName: string,
    dbName: string,
  ) =>
    api.post<{
      ok: boolean;
      db_name: string;
      aborted: number;
      skipped: number;
      errors: number;
    }>(`/storage/prepare-db/${encodeURIComponent(dbName)}/cancel`, {
      subscription_id: subscriptionId,
      storage_resource_group: storageRg,
      account_name: accountName,
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

  releaseWarmup: (body: {
    subscription_id: string;
    resource_group: string;
    aks_cluster_name: string;
    db: string;
  }) =>
    api.post<{
      db: string;
      status: "released" | "partial";
      database: string;
      namespace?: string;
      deleted?: { kind: string; selector: string; status_code: number }[];
      errors?: { kind: string; selector: string; status_code: number; detail?: string }[];
    }>("/warmup/release", body),

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

  saveAutoWarmupPreference: (body: AutoWarmupPreference) =>
    api.put<{ status: string; preference: AutoWarmupPreference }>(
      "/warmup/auto-preference",
      body,
    ),

  /** Per-process API request metrics. Window in seconds (60..86400). */
  requestMetrics: (
    params: {
      windowSeconds?: number;
      pathPrefix?: string;
      rpmBuckets?: number;
    } = {},
  ) => {
    const w = params.windowSeconds ?? 900;
    const b = params.rpmBuckets ?? 60;
    const pp = params.pathPrefix
      ? `&path_prefix=${encodeURIComponent(params.pathPrefix)}`
      : "";
    return api.get<RequestMetricsSummary>(
      `/monitor/metrics?window_seconds=${w}&rpm_buckets=${b}${pp}`,
    );
  },

  /** Most recent N captured requests (newest first) for the HTTP inspector
   * panel on the SidecarsCard. Sensitive headers (Authorization, Cookie,
   * X-Api-Key, …) are redacted server-side at capture time. */
  sidecarRequests: (limit: number = 200) =>
    api.get<SidecarRequestsResponse>(
      `/monitor/sidecar-requests?limit=${Math.max(1, Math.min(1000, limit))}`,
    ),

  /** k8s events for the cluster (optionally namespace-scoped). */
  aksEvents: (
    subscriptionId: string,
    rg: string,
    clusterName: string,
    opts: { namespace?: string; limit?: number } = {},
  ) => {
    const ns = opts.namespace ? `&namespace=${encodeURIComponent(opts.namespace)}` : "";
    const lim = opts.limit ?? 30;
    return api.get<{ events: K8sEvent[]; degraded?: boolean; degraded_reason?: string }>(
      `/monitor/aks/events?subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(rg)}&cluster_name=${encodeURIComponent(clusterName)}&limit=${lim}${ns}`,
    );
  },
};

export interface RequestMetricsSummary {
  window_seconds: number;
  now_ts: number;
  path_prefix: string | null;
  total: number;
  errors: number;
  error_rate: number;
  p50_ms: number | null;
  p95_ms: number | null;
  p99_ms: number | null;
  rpm: { t_end: number; count: number }[];
  by_path: { path: string; count: number; errors: number; p95_ms: number | null }[];
  degraded?: boolean;
  degraded_reason?: string;
}

export interface SidecarRequestHeader {
  name: string;
  value: string;
}

export interface SidecarRequestSample {
  ts: number;
  request_id: string;
  method: string;
  path: string;
  status: number;
  duration_ms: number;
  caller: string | null;
  client_ip: string | null;
  request_headers: SidecarRequestHeader[];
  request_body: string | null;
  request_body_truncated: boolean;
  response_headers: SidecarRequestHeader[];
  response_body: string | null;
  response_body_truncated: boolean;
  response_size_bytes: number | null;
}

export interface SidecarRequestsResponse {
  items: SidecarRequestSample[];
  count: number;
  capacity: number;
}

export interface K8sEvent {
  namespace: string;
  name: string;
  type: "Normal" | "Warning" | string;
  reason: string;
  message: string;
  count: number;
  last_timestamp: string;
  involved_kind: string;
  involved_name: string;
  source_component: string;
  source_host: string;
}
