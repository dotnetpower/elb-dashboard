import { api, fetchApiRaw } from "@/api/client";
import type { OrchestrationStatus } from "@/api/shared";

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
  low_complexity_filter?: boolean;
  additional_options?: string;
  taxid?: number;
  is_inclusive?: boolean;
  machine_type?: string;
  num_nodes?: number;
  pd_size?: string;
  mem_request?: string;
  mem_limit?: string;
  batch_len?: number;
  enable_warmup?: boolean;
  /** Default true: force ElasticBLAST onto the AKS node-local SSD init path. */
  use_local_ssd?: boolean;
  reuse?: boolean;
  /** Experimental: partitioned DB search is not full-DB result equivalent yet. */
  db_auto_partition?: boolean;
  sharding_mode?: "off" | "approximate" | "precise";
  db_effective_search_space?: number;
  db_total_bytes?: number;
  db_total_letters?: number;
  query_effective_search_spaces?: number[];
  query_count?: number;
  shard_sets?: number[];
  /** Legacy opt-out retained for older callers; automatic sharding is off by default. */
  disable_sharding?: boolean;
  /** Required to enable any approximate sharded/partitioned BLAST path. */
  allow_approximate_sharding?: boolean;
  db_partitions?: number;
  db_partition_prefix?: string;
  acr_resource_group?: string;
  acr_name?: string;
  storage_account?: string;
  aks_cluster_name?: string;
  terminal_resource_group?: string;
  terminal_vm_name?: string;
  tie_order_oracle_accessions?: string[];
  tie_order_oracle_text?: string;
  tie_order_oracle_strict?: boolean;
  use_db_order_oracle?: boolean;
}

export interface BlastSubmitResponse {
  id?: string;
  job_id: string;
  instance_id: string;
  task_id?: string;
  status?: string;
  statusQueryGetUri?: string;
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
  /**
   * Original submit body — the BlastSubmitRequest payload as it was POSTed.
   * Used by "Duplicate / Re-run" + "Export config" on BlastResults to
   * rehydrate the BlastSubmit form. May be absent on legacy jobs that
   * pre-date payload persistence; callers must null-check.
   */
  payload?: Record<string, unknown>;
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
  parent_job_id?: string;
  split_children?: {
    child_count: number;
    children_by_status: Record<string, number>;
    children?: Array<{
      job_id: string;
      status: string;
      phase?: string | null;
      group_id?: string | null;
      query_file?: string | null;
      effective_search_space?: number | null;
    }>;
  };
  /** Server-derived: completed split children count. */
  splits_done?: number;
  /** Server-derived: failed split children count. */
  splits_failed?: number;
  /** Server-derived: total split children count (mirrors split_children.child_count). */
  splits_total?: number;
  /** Display-only label for the query (filename or first sequence id). */
  query_label?: string;
  owner_upn?: string;
  error?: string;
}

export interface BlastResultFile {
  file_id?: string;
  name: string;
  size: number | null;
  last_modified: string | null;
  format?: string | null;
  source?: string | null;
}

export interface BlastDatabase {
  name: string;
  container: string;
  prefix?: string;
  source?: string;
  file_count?: number;
  total_bytes?: number;
  total_letters?: number;
  total_sequences?: number;
  web_blast_searchsp?: number;
  web_blast_searchsp_scope?: string;
  web_blast_searchsp_evidence?: string;
  last_modified?: string;
  source_version?: string;
  downloaded_at?: string;
  /** True when prepare-db has uploaded preset shard layouts for this DB. */
  sharded?: boolean;
  /** Sorted list of preset shard counts that have been pre-built (e.g. [1,2,3,4,5,6,8,10]). */
  shard_sets?: number[];
  /** True while a /shard daemon thread (or warmup auto-shard) is running. */
  sharding_in_progress?: boolean;
  /** ISO timestamp set when sharding starts; cleared on completion. */
  sharding_started_at?: string | null;
  /** Sanitised error string from the last failed sharding attempt; cleared on next start. */
  sharding_error?: string | null;
  db_order_oracle?: {
    status: "ready" | "building" | "failed" | string;
    run_id?: string | null;
    started_at?: string | null;
    source_version?: string | null;
    expected_parts?: number;
    ready_parts?: number;
    part_prefix?: string | null;
  };
  /**
   * Server-computed warmup feasibility. Only present when listDatabases was
   * called with cluster topology (num_nodes + machine_type). See Phase 1 of
   * the warmup pipeline (api/services/warmup_planner.py).
   */
  warmup_plan?: BlastWarmupPlan;
}

export interface TaxonomySearchResult {
  taxid: number;
  scientific_name: string;
  common_name?: string | null;
  rank: string;
  /** Optional NCBI division (e.g. "Primates") — present when esummary returns it. */
  division?: string | null;
  /** Empty in the list payload; populated by `getTaxonomyDetail` on selection. */
  lineage: string;
  matched_name: string;
  synonyms: string[];
}

export interface TaxonomySearchResponse {
  query: string;
  count: number;
  source: "ncbi_eutils";
  cached: boolean;
  results: TaxonomySearchResult[];
}

export interface TaxonomyLineageNode {
  taxid: number;
  scientific_name: string;
  rank: string;
}

export interface TaxonomyDetail {
  taxid: number;
  scientific_name: string;
  common_name: string | null;
  rank: string;
  division: string | null;
  parent_taxid: number | null;
  authority: string | null;
  synonyms: string[];
  equivalent_names: string[];
  misspellings: string[];
  lineage: string;
  lineage_ex: TaxonomyLineageNode[];
  genetic_code: string | null;
  genetic_code_id: number | null;
  mito_genetic_code: string | null;
  mito_genetic_code_id: number | null;
  create_date: string | null;
  update_date: string | null;
  pub_date: string | null;
  source: "ncbi_eutils";
  cached: boolean;
}

export interface TaxonomyImageResponse {
  name: string;
  /** Wikipedia upload.wikimedia.org thumbnail URL, or null when unavailable. */
  image_url: string | null;
  /** Wikipedia article URL, or null when not found. */
  page_url: string | null;
  source: "wikipedia";
  cached: boolean;
}

export interface TaxonomyTreeResponse {
  taxid: number;
  scientific_name: string;
  rank: string;
  lineage: TaxonomyLineageNode[];
  /** Keyed by parent taxid — sibling taxa at the same rank as the lineage child. */
  siblings: Record<string, TaxonomyLineageNode[]>;
  cached: boolean;
  source: "ncbi_eutils";
}

export type BlastWarmupStatus =
  | "ok"
  | "ok_unknown_sku"
  | "no_db_size"
  | "no_nodes"
  | "node_sku_too_small"
  | "cluster_too_small";

export interface BlastWarmupPlan {
  feasible: boolean;
  status: BlastWarmupStatus;
  message: string;
  num_nodes: number;
  machine_type: string;
  node_ram_gib: number;
  safe_node_budget_gib: number;
  db_total_bytes: number;
  db_gib: number;
  chosen_shards: number;
  target_shards: number;
  per_shard_gib: number;
  per_node_gib: number;
  shards_per_node: number;
  recommendations: string[];
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

function filenameFromDisposition(value: string | null): string | null {
  if (!value) return null;
  const match = value.match(/filename\*?=(?:UTF-8''|\")?([^";]+)/i);
  if (!match) return null;
  return decodeURIComponent(match[1].replace(/"$/, "").trim());
}

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
    additional_options?: string;
    taxid?: number;
    is_inclusive?: boolean;
    allow_approximate_sharding?: boolean;
    db_auto_partition?: boolean;
    db_total_bytes?: number;
    db_total_letters?: number;
    db_effective_search_space?: number;
    disable_sharding?: boolean;
    enable_warmup?: boolean;
    evalue?: number;
    max_target_seqs?: number;
    outfmt?: number;
    query_data?: string;
    shard_sets?: number[];
    sharding_mode?: "off" | "approximate" | "precise";
    word_size?: number;
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
        precision?: {
          requested_mode: "off" | "approximate" | "precise";
          effective_mode: "off" | "approximate" | "precise";
          precision_level:
            | "full"
            | "precise_single_query"
            | "precise_tabular"
            | "precise_tabular_split"
            | "precise_xml"
            | "precise_xml_split"
            | "approximate"
            | "blocked";
          eligible: boolean;
          merge_strategy: string;
          required_options: Record<string, unknown>;
          blocking_errors: string[];
          warnings: string[];
        };
        query_metadata?: {
          query_count: number;
          total_letters: number;
          min_length: number;
          max_length: number;
          mixed_lengths: boolean;
          records: Array<{ query_id: string; length: number }>;
        } | null;
      }>;
      critical_blockers: number;
      summary: string;
    }>("/blast/pre-flight", req),

  submit: (req: BlastSubmitRequest) =>
    api.post<BlastSubmitResponse>("/blast/jobs", req),

  searchTaxonomy: (query: string, limit = 10) =>
    api.get<TaxonomySearchResponse>(
      `/blast/taxonomy/search?q=${encodeURIComponent(query)}&limit=${encodeURIComponent(String(limit))}`,
    ),

  getTaxonomyDetail: (taxid: number) =>
    api.get<TaxonomyDetail>(`/blast/taxonomy/detail/${encodeURIComponent(String(taxid))}`),

  getTaxonomyImage: (scientificName: string) =>
    api.get<TaxonomyImageResponse>(
      `/blast/taxonomy/image?name=${encodeURIComponent(scientificName)}`,
    ),

  getTaxonomyTree: (taxid: number, siblingLimit = 3) =>
    api.get<TaxonomyTreeResponse>(
      `/blast/taxonomy/tree/${encodeURIComponent(String(taxid))}?sibling_limit=${siblingLimit}`,
    ),

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

  downloadResultFile: async (
    jobId: string,
    fileId: string,
    subscriptionId: string,
    storageAccount: string,
  ) => {
    const response = await fetchApiRaw(
      `/blast/jobs/${encodeURIComponent(jobId)}/results/${encodeURIComponent(fileId)}?subscription_id=${encodeURIComponent(subscriptionId)}&storage_account=${encodeURIComponent(storageAccount)}`,
    );
    if (!response.ok) {
      const text = await response.text();
      throw new Error(text || `HTTP ${response.status}`);
    }
    return {
      blob: await response.blob(),
      filename: filenameFromDisposition(response.headers.get("Content-Disposition")),
      contentType: response.headers.get("Content-Type") ?? "application/octet-stream",
    };
  },

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
    clusterTopology?: { numNodes: number; machineType: string },
  ) => {
    let qs =
      `/blast/databases?subscription_id=${encodeURIComponent(subscriptionId)}` +
      `&storage_account=${encodeURIComponent(storageAccount)}` +
      `&resource_group=${encodeURIComponent(resourceGroup)}`;
    if (clusterTopology && clusterTopology.numNodes > 0 && clusterTopology.machineType) {
      qs +=
        `&num_nodes=${encodeURIComponent(String(clusterTopology.numNodes))}` +
        `&machine_type=${encodeURIComponent(clusterTopology.machineType)}`;
    }
    return api.get<{
      databases: BlastDatabase[];
      public_access_disabled?: boolean;
      message?: string;
    }>(qs);
  },

  checkUpdates: () =>
    api.get<{ latest_version: string }>("/blast/databases/check-updates"),

  /**
   * Trigger prepare-db's sharding step against an already-downloaded DB.
   *
   * **Async** — returns 202 immediately; the backend runs
   * ``ensure_shard_sets`` in a daemon thread. Progress is observed by
   * polling ``listDatabases`` and reading
   * ``sharding_in_progress`` / ``sharded`` / ``sharding_error`` on the
   * matching ``BlastDatabase`` row. Re-clicking while already running
   * returns 409 (idempotent guard).
   */
  shardDatabase: (
    subscriptionId: string,
    resourceGroup: string,
    storageAccount: string,
    dbName: string,
  ) =>
    api.post<{
      accepted: boolean;
      db_name: string;
      sharding_started_at: string;
      output: string;
    }>(`/blast/databases/${encodeURIComponent(dbName)}/shard`, {
      subscription_id: subscriptionId,
      resource_group: resourceGroup,
      account_name: storageAccount,
    }),

  buildDbOrderOracle: (body: {
    subscription_id: string;
    resource_group: string;
    account_name: string;
    cluster_name: string;
    acr_name?: string;
    image?: string;
    source_version?: string;
  }, dbName: string) =>
    api.post<{
      accepted: boolean;
      db_name: string;
      run_id: string;
      expected_parts: number;
      created: string[];
      existing: string[];
      status_blob: string;
      part_urls: string[];
    }>(`/blast/databases/${encodeURIComponent(dbName)}/oracle`, body),

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
      degraded?: boolean;
      degraded_reason?: string;
      files_parsed?: number;
      total_files?: number;
      read_failures?: number;
      truncated?: boolean;
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