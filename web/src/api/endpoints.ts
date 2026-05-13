import { api } from "@/api/client";

export interface ProvisionTerminalRequest {
  subscription_id: string;
  resource_group?: string;
  region?: string;
  vm_name?: string;
  vm_size?: string;
  admin_username?: string;
  allowed_ssh_cidr: string;
  // RBAC auto-assignment fields (optional)
  workload_resource_group?: string;
  acr_resource_group?: string;
  acr_name?: string;
  storage_account?: string;
  storage_resource_group?: string;
}

export interface ProvisionTerminalStarted {
  id: string;
  statusQueryGetUri: string;
  sendEventPostUri: string;
  terminatePostUri: string;
}

export interface OrchestrationStatus<TOutput = unknown> {
  instance_id: string;
  runtime_status: string;
  custom_status: unknown;
  created_time: string;
  last_updated_time: string;
  output: TOutput | null;
}

export interface TerminalConnectionInfo {
  vm_name: string;
  resource_group: string;
  subscription_id?: string;
  region: string;
  fqdn: string;
  ssh_host: string;
  ssh_port: number;
  username: string;
  password_secret_uri: string;
  cloud_init_status: string;
}

export const terminalApi = {
  provision: (req: ProvisionTerminalRequest) =>
    api.post<ProvisionTerminalStarted>("/terminal/provision", req),
  status: (instanceId: string) =>
    api.get<OrchestrationStatus<TerminalConnectionInfo>>(
      `/terminal/status/${instanceId}`,
    ),
  password: (vmName: string, subscriptionId?: string, resourceGroup?: string) => {
    const params = new URLSearchParams();
    if (subscriptionId) params.set("subscription_id", subscriptionId);
    if (resourceGroup) params.set("resource_group", resourceGroup);
    const qs = params.toString();
    return api.get<{ vm_name: string; password: string }>(
      `/terminal/${encodeURIComponent(vmName)}/password${qs ? `?${qs}` : ""}`,
    );
  },
  openSsh: (
    vmName: string,
    callerIp: string,
    subscriptionId: string,
    resourceGroup: string,
  ) =>
    api.post<{ ok: boolean; nsg: string; allowed_ip: string }>(
      `/terminal/${encodeURIComponent(vmName)}/open-ssh?caller_ip=${encodeURIComponent(callerIp)}&subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(resourceGroup)}`,
      {},
    ),
  stopVm: (vmName: string, subscriptionId: string, resourceGroup: string) =>
    api.post<{ ok: boolean; vm_name: string; status: string }>(
      `/terminal/${encodeURIComponent(vmName)}/stop?subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(resourceGroup)}`,
      {},
    ),
  startVm: (vmName: string, subscriptionId: string, resourceGroup: string) =>
    api.post<{ ok: boolean; vm_name: string; status: string }>(
      `/terminal/${encodeURIComponent(vmName)}/start?subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(resourceGroup)}`,
      {},
    ),
  health: (vmName: string, subscriptionId: string, resourceGroup: string) =>
    api.get<TerminalHealth>(
      `/terminal/${encodeURIComponent(vmName)}/health?subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(resourceGroup)}`,
    ),
};

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

export interface TerminalHealth {
  az_cli: string;
  kubectl: string;
  azcopy: string;
  python: string;
  az_login_active: boolean;
  az_login_user: string;
  az_login_age_seconds: number;
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

  // Direct K8s API (fast, ~1-3s)
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
};

// ---------------------------------------------------------------------------
// AKS Cluster provisioning
// ---------------------------------------------------------------------------
export interface AksProvisionRequest {
  subscription_id: string;
  resource_group: string;
  region: string;
  cluster_name: string;
  node_sku?: string;
  node_count?: number;
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
  status: string;
  message: string;
  roles_assigned?: string[];
}

export const aksApi = {
  listSkus: () => api.get<{ skus: string[]; default: string }>("/aks/skus"),

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

  proxyOpenApiSpec: (subscriptionId: string, rg: string, clusterName: string) =>
    api.get<Record<string, unknown>>(
      `/aks/openapi/spec?subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(rg)}&cluster_name=${encodeURIComponent(clusterName)}`,
    ),
};

// ---------------------------------------------------------------------------
// BLAST
// ---------------------------------------------------------------------------

export type BlastProgram =
  | "blastn"
  | "blastp"
  | "blastx"
  | "tblastn"
  | "tblastx"
  | "psiblast"
  | "rpsblast"
  | "rpstblastn";

export interface BlastSubmitRequest {
  subscription_id: string;
  resource_group: string;
  region?: string;
  program: BlastProgram;
  db: string;
  query_data?: string;
  query_blob_url?: string;
  job_title?: string;
  evalue?: number;
  max_target_seqs?: number;
  outfmt?: number;
  word_size?: number;
  gap_open?: number;
  gap_extend?: number;
  additional_options?: string;
  machine_type?: string;
  num_nodes?: number;
  pd_size?: string;
  mem_request?: string;
  mem_limit?: string;
  batch_len?: number;
  enable_warmup?: boolean;
  reuse?: boolean;
  db_auto_partition?: boolean;
  db_partitions?: number;
  db_partition_prefix?: string;
  acr_resource_group?: string;
  acr_name?: string;
  storage_account?: string;
  aks_cluster_name?: string;
  terminal_resource_group?: string;
  terminal_vm_name?: string;
}

export interface BlastSubmitResponse {
  job_id: string;
  instance_id: string;
}

export interface BlastJobSummary {
  job_id: string;
  instance_id?: string;
  job_title: string;
  program: string;
  db: string;
  status: string;
  phase: string;
  created_at: string;
  updated_at: string;
  runtime_status?: string;
  custom_status?: unknown;
  output?: unknown;
  config_snapshot?: Record<string, unknown>;
  infrastructure?: {
    subscription_id?: string;
    resource_group?: string;
    region?: string;
    storage_account?: string;
    acr_name?: string;
    cluster_name?: string;
    terminal_vm?: string;
  };
  owner_upn?: string;
  error?: string;
}

export interface BlastResultFile {
  name: string;
  size: number | null;
  last_modified: string | null;
}

export interface BlastDatabase {
  name: string;
  container: string;
  prefix?: string;
  source?: string;
  file_count?: number;
  total_bytes?: number;
  last_modified?: string;
  source_version?: string;
  downloaded_at?: string;
}

export interface BlastHit {
  qseqid: string;
  sseqid: string;
  pident: number;
  length: number;
  mismatch: number;
  gapopen: number;
  qstart: number;
  qend: number;
  sstart: number;
  send: number;
  evalue: number;
  bitscore: number;
  qseq?: string;
  sseq?: string;
  qlen?: number;
  slen?: number;
  ppos?: number;
}

export interface BlastAggregateStats {
  total_hits: number;
  unique_queries: number;
  unique_subjects: number;
  evalue_distribution: Record<string, number>;
  identity_distribution: Record<string, number>;
  top_subjects: Array<{ id: string; count: number }>;
  avg_identity: number | null;
  avg_bitscore: number | null;
  avg_length: number | null;
  max_bitscore: number | null;
  min_evalue: number | null;
  files_parsed?: number;
  total_files?: number;
}

export type BlastExportFormat = "csv" | "tsv" | "json";

export const blastApi = {
  preFlight: (req: {
    subscription_id: string;
    resource_group: string;
    acr_resource_group?: string;
    acr_name?: string;
    storage_account: string;
    aks_cluster_name: string;
    terminal_resource_group?: string;
    terminal_vm_name?: string;
    db: string;
    query_data?: string;
  }) =>
    api.post<{
      ready: boolean;
      checks: Array<{
        id: string;
        status: "pass" | "fail" | "warn" | "skip";
        title: string;
        detail?: string;
        action?: string;
        action_type?: string;
        action_params?: Record<string, string>;
        severity?: string;
        suggested_dbs?: string[];
      }>;
      critical_blockers: number;
      summary: string;
    }>("/blast/pre-flight", req),

  submit: (req: BlastSubmitRequest) =>
    api.post<BlastSubmitResponse>("/blast/submit", req),

  submitStatus: (instanceId: string) =>
    api.get<OrchestrationStatus<unknown>>(
      `/blast/submit/${encodeURIComponent(instanceId)}/status`,
    ),

  uploadQuery: (data: {
    subscription_id: string;
    storage_account: string;
    query_data: string;
    resource_group?: string;
    container?: string;
    filename?: string;
  }) => api.post<{ blob_url: string; blob_path: string }>("/blast/upload-query", data),

  listJobs: () => api.get<{ jobs: BlastJobSummary[] }>("/blast/jobs"),

  getJob: (jobId: string, history = false) =>
    api.get<BlastJobSummary>(
      `/blast/jobs/${encodeURIComponent(jobId)}${history ? "?history=1" : ""}`,
    ),

  cancelJob: (jobId: string) =>
    api.post<{ job_id: string; status: string }>(
      `/blast/jobs/${encodeURIComponent(jobId)}/cancel`,
      {},
    ),

  deleteJob: (jobId: string) =>
    api.del<{ job_id: string; status: string }>(
      `/blast/jobs/${encodeURIComponent(jobId)}`,
    ),

  readJobFile: (
    jobId: string,
    filename: string,
    subscriptionId: string,
    storageAccount: string,
    maxBytes = 4096,
  ) =>
    api.get<{ name: string; content: string; truncated: boolean }>(
      `/blast/jobs/${encodeURIComponent(jobId)}/file?name=${encodeURIComponent(filename)}&subscription_id=${encodeURIComponent(subscriptionId)}&storage_account=${encodeURIComponent(storageAccount)}&max_bytes=${maxBytes}`,
    ),

  listResults: (
    jobId: string,
    subscriptionId: string,
    storageAccount: string,
    resourceGroup?: string,
  ) =>
    api.get<{
      job_id: string;
      files: BlastResultFile[];
      public_access_disabled?: boolean;
      message?: string;
    }>(
      `/blast/jobs/${encodeURIComponent(jobId)}/results?subscription_id=${encodeURIComponent(subscriptionId)}&storage_account=${encodeURIComponent(storageAccount)}${resourceGroup ? `&resource_group=${encodeURIComponent(resourceGroup)}` : ""}`,
    ),

  downloadResult: (
    jobId: string,
    subscriptionId: string,
    storageAccount: string,
    blobName: string,
  ) =>
    api.get<{ download_url: string }>(
      `/blast/jobs/${encodeURIComponent(jobId)}/results/download?subscription_id=${encodeURIComponent(subscriptionId)}&storage_account=${encodeURIComponent(storageAccount)}&blob_name=${encodeURIComponent(blobName)}`,
    ),

  exportResults: (
    jobId: string,
    subscriptionId: string,
    storageAccount: string,
    format: BlastExportFormat,
  ) =>
    api.getText(
      `/blast/jobs/${encodeURIComponent(jobId)}/results/export?subscription_id=${encodeURIComponent(subscriptionId)}&storage_account=${encodeURIComponent(storageAccount)}&format=${format}`,
    ),

  listDatabases: (
    subscriptionId: string,
    storageAccount: string,
    resourceGroup: string,
  ) =>
    api.get<{
      databases: BlastDatabase[];
      public_access_disabled?: boolean;
      message?: string;
    }>(
      `/blast/databases?subscription_id=${encodeURIComponent(subscriptionId)}&storage_account=${encodeURIComponent(storageAccount)}&resource_group=${encodeURIComponent(resourceGroup)}`,
    ),

  checkUpdates: () =>
    api.get<{ latest_version: string }>("/blast/databases/check-updates"),

  buildCustomDb: (req: {
    subscription_id: string;
    resource_group: string;
    storage_account: string;
    terminal_resource_group?: string;
    terminal_vm_name?: string;
    db_name: string;
    db_type: "nucl" | "prot";
    title?: string;
    fasta_data?: string;
    fasta_blob_url?: string;
  }) =>
    api.post<{
      db_name: string;
      db_type: string;
      title: string;
      status: string;
      file_count: number;
      container: string;
      path: string;
    }>("/blast/databases/build", req),

  resultsAggregate: (jobId: string, subscriptionId: string, storageAccount: string) =>
    api.get<{
      job_id: string;
      status: string;
      message?: string;
      stats: BlastAggregateStats | null;
    }>(
      `/blast/jobs/${encodeURIComponent(jobId)}/results/aggregate?subscription_id=${encodeURIComponent(subscriptionId)}&storage_account=${encodeURIComponent(storageAccount)}`,
    ),

  resultsAlignments: (
    jobId: string,
    subscriptionId: string,
    storageAccount: string,
    opts?: { blob_name?: string; max_alignments?: number; query_id?: string },
  ) =>
    api.get<{
      job_id: string;
      blob_name: string;
      alignments: BlastHit[];
      total_hits: number;
      returned: number;
      query_ids: string[];
    }>(
      `/blast/jobs/${encodeURIComponent(jobId)}/results/alignments?subscription_id=${encodeURIComponent(subscriptionId)}&storage_account=${encodeURIComponent(storageAccount)}${opts?.blob_name ? `&blob_name=${encodeURIComponent(opts.blob_name)}` : ""}${opts?.max_alignments ? `&max_alignments=${opts.max_alignments}` : ""}${opts?.query_id ? `&query_id=${encodeURIComponent(opts.query_id)}` : ""}`,
    ),
};

// ---------------------------------------------------------------------------
// Resource provisioning (wizard)
// ---------------------------------------------------------------------------

export interface EnsureRgRequest {
  subscription_id: string;
  resource_group: string;
  region: string;
}

export interface EnsureStorageRequest {
  subscription_id: string;
  resource_group: string;
  account_name: string;
  region: string;
}

export interface EnsureAcrRequest {
  subscription_id: string;
  resource_group: string;
  registry_name: string;
  region: string;
}

export const resourceApi = {
  ensureRg: (req: EnsureRgRequest) =>
    api.post<{ resource_group: string; status: string }>("/resources/ensure-rg", req),

  ensureStorage: (req: EnsureStorageRequest) =>
    api.post<{ account_name: string; status: string }>("/resources/ensure-storage", req),

  ensureAcr: (req: EnsureAcrRequest) =>
    api.post<{ registry_name: string; status: string }>("/resources/ensure-acr", req),
};

// ---------------------------------------------------------------------------
// ARM discovery (backend-proxied — uses az login credential)
// ---------------------------------------------------------------------------
export interface ArmSubscription {
  subscriptionId: string;
  displayName: string;
  state: string;
  tenantId: string;
}

export interface ArmResourceGroup {
  name: string;
  location: string;
  tags?: Record<string, string>;
}

export interface ArmStorageAccount {
  name: string;
  location: string;
}

export interface ArmAcr {
  name: string;
  location: string;
  loginServer?: string;
}

export interface ArmVm {
  name: string;
  location: string;
}

export const armProxyApi = {
  listSubscriptions: () => api.get<ArmSubscription[]>("/arm/subscriptions"),

  listResourceGroups: (subscriptionId: string) =>
    api.get<ArmResourceGroup[]>(
      `/arm/subscriptions/${encodeURIComponent(subscriptionId)}/resource-groups`,
    ),

  listStorageAccounts: (subscriptionId: string, rg: string) =>
    api.get<ArmStorageAccount[]>(
      `/arm/subscriptions/${encodeURIComponent(subscriptionId)}/resource-groups/${encodeURIComponent(rg)}/storage-accounts`,
    ),

  listAcrs: (subscriptionId: string, rg: string) =>
    api.get<ArmAcr[]>(
      `/arm/subscriptions/${encodeURIComponent(subscriptionId)}/resource-groups/${encodeURIComponent(rg)}/acrs`,
    ),

  listVms: (subscriptionId: string, rg: string) =>
    api.get<ArmVm[]>(
      `/arm/subscriptions/${encodeURIComponent(subscriptionId)}/resource-groups/${encodeURIComponent(rg)}/vms`,
    ),

  getRgTags: (subscriptionId: string, rg: string) =>
    api.get<{ resource_group: string; tags: Record<string, string> }>(
      `/arm/resource-group/tags?subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(rg)}`,
    ),

  setRgTags: (subscriptionId: string, rg: string, tags: Record<string, string>) =>
    api.post<{ resource_group: string; tags: Record<string, string> }>(
      "/arm/resource-group/tags",
      { subscription_id: subscriptionId, resource_group: rg, tags },
    ),
};

// ---------------------------------------------------------------------------
// P6 — Report export
// ---------------------------------------------------------------------------
export const reportApi = {
  exportResults: (
    jobId: string,
    subscriptionId: string,
    storageAccount: string,
    format: BlastExportFormat,
  ) => blastApi.exportResults(jobId, subscriptionId, storageAccount, format),
  exportUrl: (
    jobId: string,
    subscriptionId: string,
    storageAccount: string,
    format: BlastExportFormat,
  ) =>
    `${import.meta.env.VITE_API_BASE_URL ?? ""}/api/blast/jobs/${encodeURIComponent(jobId)}/results/export?subscription_id=${encodeURIComponent(subscriptionId)}&storage_account=${encodeURIComponent(storageAccount)}&format=${format}`,
};

// ---------------------------------------------------------------------------
// P9 — Audit trail
// ---------------------------------------------------------------------------
export interface AuditEvent {
  action: string;
  timestamp: string;
  user?: string;
  job_id?: string;
  details?: Record<string, unknown>;
}

export const auditApi = {
  listEvents: (limit = 100, action?: string) =>
    api.get<{ events: AuditEvent[]; total: number }>(
      `/audit/log?limit=${limit}${action ? `&action=${encodeURIComponent(action)}` : ""}`,
    ),
};

// ---------------------------------------------------------------------------
// P11 — Cost estimator
// ---------------------------------------------------------------------------
export interface CostEstimate {
  compute_usd: number;
  disk_usd: number;
  storage_usd: number;
  total_usd: number;
}

export const costApi = {
  estimate: (params: {
    machine_type?: string;
    num_nodes?: number;
    estimated_hours?: number;
    pd_size_gb?: number;
    db_size_gb?: number;
  }) =>
    api.post<{
      estimate: CostEstimate;
      params: Record<string, unknown>;
      note: string;
    }>("/blast/cost-estimate", params),
};

// ---------------------------------------------------------------------------
// P8 — Multi-DB search
// ---------------------------------------------------------------------------
export const multiBlastApi = {
  submit: (req: Record<string, unknown> & { databases: string[] }) =>
    api.post<{
      group_id: string;
      jobs: Array<{ job_id: string; db: string; instance_id: string }>;
      total: number;
    }>("/blast/multi-submit", req),
};

// ---------------------------------------------------------------------------
// P4 — Taxonomy annotation
// ---------------------------------------------------------------------------
export interface TaxonomyInfo {
  accession: string;
  title: string;
  organism: string;
  taxid: string;
  source_db?: string;
  seq_length?: string;
  mol_type?: string;
  update_date?: string;
}

export const taxonomyApi = {
  lookup: (accessions: string[]) =>
    api.post<{
      annotations: Record<string, TaxonomyInfo>;
      found: number;
      requested: number;
    }>("/blast/taxonomy", { accessions }),
};

// ---------------------------------------------------------------------------
// P5 — Query preprocessor
// ---------------------------------------------------------------------------
export interface PreprocessStats {
  input_sequences: number;
  output_sequences: number;
  total_bases: number;
  filtered_short: number;
  filtered_quality: number;
  avg_length: number;
  min_len: number;
  max_len: number;
  gc_content: number;
}

export const preprocessApi = {
  process: (params: {
    input_data: string;
    format?: "auto" | "fastq" | "fasta";
    min_length?: number;
    min_quality?: number;
  }) =>
    api.post<{
      fasta_output: string;
      stats: PreprocessStats;
      detected_format: string;
    }>("/blast/preprocess", params),
};

// ---------------------------------------------------------------------------
// P2 — DB version registry
// ---------------------------------------------------------------------------
export interface DbVersionMeta {
  db_name: string;
  db_type?: string;
  title?: string;
  source?: string;
  source_version?: string;
  version_tag?: string;
  notes?: string;
  created_at?: string;
  created_by?: string;
  _blob_path?: string;
  _last_modified?: string;
}

export const dbVersionApi = {
  list: (subscriptionId: string, storageAccount: string, resourceGroup: string) =>
    api.get<{ versions: DbVersionMeta[]; total: number }>(
      `/blast/databases/versions?subscription_id=${encodeURIComponent(subscriptionId)}&storage_account=${encodeURIComponent(storageAccount)}&resource_group=${encodeURIComponent(resourceGroup)}`,
    ),

  save: (params: {
    subscription_id: string;
    storage_account: string;
    db_name: string;
    db_type?: string;
    title?: string;
    source?: string;
    source_version?: string;
    version_tag?: string;
    notes?: string;
  }) =>
    api.post<{ db_name: string; status: string; metadata: DbVersionMeta }>(
      "/blast/databases/versions",
      params,
    ),
};

// ---------------------------------------------------------------------------
// P7 — Scheduled BLAST
// ---------------------------------------------------------------------------
export interface BlastSchedule {
  schedule_id: string;
  name: string;
  trigger_type: "manual" | "cron" | "on_upload";
  cron_expression?: string;
  watch_container?: string;
  watch_prefix?: string;
  blast_params: Record<string, unknown>;
  enabled: boolean;
  created_at?: string;
  last_run?: string;
  run_count: number;
  owner_upn?: string;
}

export const scheduleApi = {
  list: () => api.get<{ schedules: BlastSchedule[] }>("/blast/schedules"),

  create: (params: Record<string, unknown> & { name: string; trigger_type: string }) =>
    api.post<{ status: string; schedule: BlastSchedule }>("/blast/schedules", params),

  remove: (scheduleId: string) =>
    api.del<{ status: string; schedule_id: string }>(
      `/blast/schedules/${encodeURIComponent(scheduleId)}`,
    ),

  run: (scheduleId: string) =>
    api.post<{ job_id: string; instance_id: string; schedule_id: string }>(
      `/blast/schedules/${encodeURIComponent(scheduleId)}/run`,
      {},
    ),
};

// ---------------------------------------------------------------------------
// P12 — Primer design
// ---------------------------------------------------------------------------
export interface PrimerPair {
  pair_index: number;
  left_sequence: string;
  right_sequence: string;
  left_tm: number | null;
  right_tm: number | null;
  left_gc: number | null;
  right_gc: number | null;
  product_size: number | null;
  pair_penalty: number | null;
  left_start?: number;
  left_length?: number;
  right_start?: number;
  right_length?: number;
}

export const primerApi = {
  design: (params: {
    sequence: string;
    subscription_id: string;
    terminal_resource_group?: string;
    terminal_vm_name?: string;
    target_start?: number;
    target_length?: number;
    product_size_min?: number;
    product_size_max?: number;
    num_return?: number;
  }) =>
    api.post<{
      primers: PrimerPair[];
      target: { start: number; length: number };
      product_size_range: string;
      sequence_length: number;
    }>("/blast/primer-design", params),
};
