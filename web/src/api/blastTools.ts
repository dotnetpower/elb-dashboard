import { api } from "@/api/client";
import { blastApi, type BlastExportFormat } from "@/api/blast";
import { apiBaseUrl } from "@/config/runtime";

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
    `${apiBaseUrl()}/api/blast/jobs/${encodeURIComponent(jobId)}/results/export?subscription_id=${encodeURIComponent(subscriptionId)}&storage_account=${encodeURIComponent(storageAccount)}&format=${format}`,
};

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

export const multiBlastApi = {
  submit: (req: Record<string, unknown> & { databases: string[] }) =>
    api.post<{
      group_id: string;
      jobs: Array<{ job_id: string; db: string; instance_id: string }>;
      total: number;
    }>("/blast/multi-submit", req),
};

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