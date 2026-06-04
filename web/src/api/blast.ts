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
  /**
   * NCBI nuccore accession (with or without `.version`). When supplied the
   * backend resolves it to FASTA via E-utilities and stages it like any
   * inline `query_data`. Ignored when `query_data` / `query_blob_url` is set.
   */
  query_accession?: string;
  /** 1-based inclusive start (only used when `query_accession` is set). */
  query_accession_seq_start?: number;
  /** 1-based inclusive end (only used when `query_accession` is set). */
  query_accession_seq_stop?: number;
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
  job_id_kind?: "dashboard" | "openapi" | string;
  dashboard_job_id?: string;
  openapi_job_id?: string | null;
  instance_id: string;
  task_id?: string;
  status?: string;
  statusQueryGetUri?: string;
  operation_status_url?: string;
  operation?: ApiOperation;
  target?: ApiTarget;
  admission?: ApiAdmission;
  meta?: ApiResponseMeta;
}

export interface ApiResponseMeta {
  request_id?: string;
  generated_at?: string;
  warnings?: Array<Record<string, unknown>>;
}

export interface ApiOperation {
  operation_id: string;
  operation_type: string;
  state: string;
  accepted_at?: string;
  poll_after_seconds?: number;
  links?: Record<string, string>;
}

export interface ApiTarget {
  resource_type: string;
  job_id: string;
  job_id_kind: string;
  dashboard_job_id?: string;
  openapi_job_id?: string | null;
  links?: Record<string, string>;
}

export interface ApiAdmission {
  decision: "accepted" | "would_accept" | "would_reject" | "rejected" | string;
  reason: string;
  basis: string;
  snapshot_at: string;
  queue?: Record<string, unknown>;
  capacity?: Record<string, unknown>;
  warnings?: Array<Record<string, unknown>>;
}

export interface BlastJobSummary {
  job_id: string;
  job_id_kind?: "dashboard" | "openapi" | string;
  dashboard_job_id?: string | null;
  openapi_job_id?: string | null;
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
  target?: ApiTarget;
  meta?: ApiResponseMeta;
  provenance?: BlastProvenanceBundle;
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
  database_metadata?: BlastDatabaseMetadata;
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
  error_code?: string;
  error?: string;
  /**
   * True when the server could not refresh this active row's live state
   * because its AKS cluster is stopped/missing. The status/phase shown is the
   * last-known value and is frozen until the cluster restarts. See
   * `refresh_blocked_reason` for why.
   */
  stale?: boolean;
  /** Why the live refresh was skipped — e.g. `cluster_stopped`, `cluster_not_found`. */
  refresh_blocked_reason?: string;
  /** ARM power_state of the job's cluster when the refresh was blocked (e.g. `Stopped`). */
  cluster_power_state?: string;
}

export interface BlastExecutionStepsSnapshot {
  schema_version: number;
  job_id: string;
  status: string;
  phase: string;
  created_at?: string | null;
  updated_at?: string | null;
  artifact_state?: "ready" | "missing" | "inline_fallback" | string;
  custom_status?: unknown;
  output?: unknown;
}

export interface BlastProvenanceBundle {
  schema_version: number;
  job_id: string;
  artifact?: { container?: string; path?: string };
  blast?: { program?: string; version?: string };
  database?: Record<string, unknown>;
  query?: Record<string, unknown>;
  options?: Record<string, unknown>;
  compatibility?: BlastCompatibilityContract | null;
  precision?: BlastPrecisionReport | null;
}

export interface BlastDatabaseMetadata {
  name: string;
  database: string;
  title?: string;
  description?: string;
  /**
   * Human label such as "mixed DNA" / "protein" — populated by the
   * dashboard's `api/services/blast/db_metadata.py`. Do NOT swap this
   * source for elb-openapi's `/v1/databases/{name}.molecule_type` field
   * (which carries the lowercase token `dna` / `protein`); use the
   * `molecule_label` field from that schema instead.
   */
  molecule_type?: string;
  update_date?: string;
  number_of_sequences?: number;
  number_of_letters?: number;
  source_version?: string;
  downloaded_at?: string;
  source?: string;
}

export interface BlastResultFile {
  file_id?: string;
  name: string;
  size: number | null;
  last_modified: string | null;
  format?: string | null;
  source?: string | null;
}

export interface BlastResultManifest {
  schema_version: number;
  job_id: string;
  status: "available" | "no_result_files" | "degraded" | string;
  source: string;
  degraded_reason?: string | null;
  file_count: number;
  parseable_count: number;
  files: Array<{
    file_id: string;
    name: string;
    size?: number | null;
    last_modified?: string | null;
    format: string;
    parseable: boolean;
    source?: string | null;
  }>;
}

export interface BlastJobEvent {
  id: string;
  job_id: string;
  event: string;
  phase: string;
  status: string;
  timestamp: string;
  payload: Record<string, unknown>;
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
  shard_source_version?: string | null;
  shards_stale?: boolean;
  /** True while a /shard daemon thread (or warmup auto-shard) is running. */
  sharding_in_progress?: boolean;
  /** ISO timestamp set when sharding starts; cleared on completion. */
  sharding_started_at?: string | null;
  /** Sanitised error string from the last failed sharding attempt; cleared on next start. */
  sharding_error?: string | null;
  update_in_progress?: boolean;
  updating_to_source_version?: string | null;
  update_started_at?: string | null;
  update_completed_at?: string | null;
  update_error?: string | null;
  update_failed_at?: string | null;
  /**
   * Honest per-DB copy lifecycle from the hardened prepare-db pipeline.
   * Replaces the legacy "file_count >= 90% of expected" SPA heuristic which
   * could mark partial copies as Ready (see ETag-based partial detection).
   * Phases: `copying` (start_copy_from_url polling), `partial` (one or more
   * server-side copies failed/aborted/timed out), `init_failed` (initiation
   * itself errored), `completed` (every copy reached `copy.status=success`).
   */
  copy_status?: {
    phase: "copying" | "partial" | "init_failed" | "completed" | string;
    total_files?: number;
    success?: number;
    failed?: number;
    aborted?: number;
    pending?: number;
    timed_out?: boolean;
    initiation_started?: number;
    initiation_skipped?: number;
    initiation_errors?: number;
    /** AKS-fanout only: total bytes landed so far (drives download speed). */
    bytes_done?: number;
  };
  /** Per-blob failure details from copy.status polling (truncated to 50). */
  failed_files?: Array<{ blob: string; status: string; reason?: string }>;
  /**
   * NCBI ETag (md5 or first tar.gz) captured at the end of prepare-db.
   * Used by the per-DB update check to differentiate "real DB changed" from
   * "NCBI rotated latest-dir but this DB's bytes did not".
   */
  signature_etag?: string;
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
   * the warmup pipeline (api/services/warmup/planner.py).
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

export interface BlastPrecisionReport {
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
}

export interface BlastCompatibilityContract {
  mode: "precise" | "calibration_required" | "approximate";
  level: string;
  eligible: boolean;
  database: string;
  search_space_source: string;
  searchsp?: number | null;
  evidence?: Record<string, unknown> | null;
  precision?: BlastPrecisionReport | null;
  blocking_errors: string[];
  warnings: string[];
}

export type BlastHitNumeric = number | string;

export interface BlastHit {
  qseqid: string;
  sseqid: string;
  pident: BlastHitNumeric;
  length: BlastHitNumeric;
  mismatch: BlastHitNumeric;
  gapopen: BlastHitNumeric;
  qstart: BlastHitNumeric;
  qend: BlastHitNumeric;
  sstart: BlastHitNumeric;
  send: BlastHitNumeric;
  evalue: BlastHitNumeric;
  bitscore: BlastHitNumeric;
  score?: BlastHitNumeric;
  gaps?: BlastHitNumeric;
  qseq?: string;
  sseq?: string;
  midline?: string;
  qlen?: BlastHitNumeric;
  slen?: BlastHitNumeric;
  qcovs?: BlastHitNumeric;
  scovs?: BlastHitNumeric;
  ppos?: BlastHitNumeric;
  /**
   * Reading frame for translated BLAST programs (blastx / tblastn /
   * tblastx). Values are in {-3,-2,-1,1,2,3}. Absent for blastn / blastp
   * because the parser drops zero-valued frames (see results_parser).
   */
  qframe?: BlastHitNumeric;
  sframe?: BlastHitNumeric;
  stitle?: string;
  sscinames?: string;
  staxids?: string;
  source_blob?: string;
  review_status?:
    | "strong_match"
    | "review_priority"
    | "low_confidence"
    | "weak_hit"
    | "unclassified";
  review_reason?: string;
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

export type BlastExportFormat =
  | "csv"
  | "tsv"
  | "json"
  | "hit-table-text"
  | "hit-table-csv"
  | "json-seqalign"
  | "ncbi-hit-table-text"
  | "ncbi-hit-table-csv"
  | "ncbi-report-text"
  | "xml"
  | "text";

export type BlastCitationFormat = "text" | "markdown" | "bibtex";

export interface BlastCitation {
  job_id: string;
  format: BlastCitationFormat;
  citation: string;
  rid: string;
  program: string;
  blast_version: string;
  database: string;
  database_snapshot?: string | null;
  search_space?: string | null;
}

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
    query_effective_search_spaces?: number[];
    query_count?: number;
    shard_sets?: number[];
    sharding_mode?: "off" | "approximate" | "precise";
    word_size?: number;
  }) =>
    api.post<{
      status?: "ok" | string;
      ready: boolean;
      decision?: "would_accept" | "would_reject" | string;
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
        precision?: BlastPrecisionReport;
        compatibility?: BlastCompatibilityContract;
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
      compatibility?: BlastCompatibilityContract | null;
      admission?: ApiAdmission;
      meta?: ApiResponseMeta;
    }>("/blast/pre-flight", req),

  submit: (req: BlastSubmitRequest) => api.post<BlastSubmitResponse>("/blast/jobs", req),

  searchTaxonomy: (query: string, limit = 10) =>
    api.get<TaxonomySearchResponse>(
      `/blast/taxonomy/search?q=${encodeURIComponent(query)}&limit=${encodeURIComponent(String(limit))}`,
    ),

  getTaxonomyDetail: (taxid: number) =>
    api.get<TaxonomyDetail>(
      `/blast/taxonomy/detail/${encodeURIComponent(String(taxid))}`,
    ),

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

  listJobs: (context?: {
    subscriptionId?: string;
    resourceGroup?: string;
    clusterName?: string;
  }) => {
    const params = new URLSearchParams();
    if (context?.subscriptionId) params.set("subscription_id", context.subscriptionId);
    if (context?.resourceGroup) params.set("resource_group", context.resourceGroup);
    if (context?.clusterName) params.set("cluster_name", context.clusterName);
    const qs = params.toString();
    return api.get<{ jobs: BlastJobSummary[] }>(`/blast/jobs${qs ? `?${qs}` : ""}`);
  },

  getJob: (
    jobId: string,
    options?: { history?: boolean; includeDatabaseMetadata?: boolean },
  ) => {
    const params = new URLSearchParams();
    if (options?.history) params.set("history", "1");
    if (options?.includeDatabaseMetadata === false) {
      params.set("include_database_metadata", "false");
    }
    const qs = params.toString();
    return api.get<BlastJobSummary>(
      `/blast/jobs/${encodeURIComponent(jobId)}${qs ? `?${qs}` : ""}`,
    );
  },

  getExecutionSteps: (jobId: string) =>
    api.get<BlastExecutionStepsSnapshot>(
      `/blast/jobs/${encodeURIComponent(jobId)}/execution-steps`,
    ),

  createLogStreamTicket: (
    jobId: string,
    context?: {
      subscriptionId?: string;
      resourceGroup?: string;
      clusterName?: string;
      namespace?: string;
      tailLines?: number;
    },
  ) =>
    api.post<{ ticket: string; ttl_seconds: number }>(
      `/blast/logs/${encodeURIComponent(jobId)}/ticket`,
      {
        subscription_id: context?.subscriptionId,
        resource_group: context?.resourceGroup,
        cluster_name: context?.clusterName,
        namespace: context?.namespace ?? "default",
        tail_lines: context?.tailLines ?? 120,
      },
    ),

  cancelJob: (
    jobId: string,
    context?: {
      subscriptionId?: string;
      resourceGroup?: string;
      clusterName?: string;
      storageAccount?: string;
    },
  ) =>
    api.post<{ job_id: string; status: string }>(
      `/blast/jobs/${encodeURIComponent(jobId)}/cancel`,
      {
        subscription_id: context?.subscriptionId,
        resource_group: context?.resourceGroup,
        cluster_name: context?.clusterName,
        storage_account: context?.storageAccount,
      },
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
    blobName?: string,
    resourceGroup?: string,
  ) =>
    api.get<{ name: string; content: string; truncated: boolean }>(
      `/blast/jobs/${encodeURIComponent(jobId)}/file?name=${encodeURIComponent(blobName || filename)}&subscription_id=${encodeURIComponent(subscriptionId)}&storage_account=${encodeURIComponent(storageAccount)}&max_bytes=${maxBytes}${resourceGroup ? `&resource_group=${encodeURIComponent(resourceGroup)}` : ""}`,
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
      manifest?: BlastResultManifest;
      public_access_disabled?: boolean;
      message?: string;
    }>(
      `/blast/jobs/${encodeURIComponent(jobId)}/results?subscription_id=${encodeURIComponent(subscriptionId)}&storage_account=${encodeURIComponent(storageAccount)}${resourceGroup ? `&resource_group=${encodeURIComponent(resourceGroup)}` : ""}`,
    ),

  listEvents: (jobId: string) =>
    api.get<{ job_id: string; events: BlastJobEvent[] }>(
      `/blast/jobs/${encodeURIComponent(jobId)}/events`,
    ),

  /**
   * Fetch the original FASTA submitted with this job, streamed through the
   * api sidecar. Used by the Edit search button to rehydrate the form
   * because the dashboard strips ``query_data`` from the persisted payload
   * after upload. The backend enforces a 5 MiB cap and returns 413 with
   * code ``query_too_large_for_edit`` if exceeded.
   */
  getQuery: (jobId: string) =>
    api.get<{
      job_id: string;
      query_text: string;
      size_bytes: number;
      max_bytes: number;
    }>(`/blast/jobs/${encodeURIComponent(jobId)}/query`),

  downloadResult: (
    jobId: string,
    subscriptionId: string,
    storageAccount: string,
    blobName: string,
    resourceGroup?: string,
  ) =>
    api.get<{ download_url: string }>(
      `/blast/jobs/${encodeURIComponent(jobId)}/results/download?subscription_id=${encodeURIComponent(subscriptionId)}&storage_account=${encodeURIComponent(storageAccount)}&blob_name=${encodeURIComponent(blobName)}${resourceGroup ? `&resource_group=${encodeURIComponent(resourceGroup)}` : ""}`,
    ),

  downloadResultFile: async (
    jobId: string,
    fileId: string,
    subscriptionId: string,
    storageAccount: string,
    resourceGroup?: string,
  ) => {
    const response = await fetchApiRaw(
      `/blast/jobs/${encodeURIComponent(jobId)}/results/${encodeURIComponent(fileId)}?subscription_id=${encodeURIComponent(subscriptionId)}&storage_account=${encodeURIComponent(storageAccount)}${resourceGroup ? `&resource_group=${encodeURIComponent(resourceGroup)}` : ""}`,
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
    resourceGroup?: string,
  ) =>
    api.getText(
      `/blast/jobs/${encodeURIComponent(jobId)}/results/export?subscription_id=${encodeURIComponent(subscriptionId)}&storage_account=${encodeURIComponent(storageAccount)}&format=${format}${resourceGroup ? `&resource_group=${encodeURIComponent(resourceGroup)}` : ""}`,
    ),

  getCitation: (jobId: string, format: BlastCitationFormat = "text") =>
    api.get<BlastCitation>(
      `/blast/jobs/${encodeURIComponent(jobId)}/citation?format=${encodeURIComponent(format)}`,
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

  checkUpdates: (
    options?: {
      subscriptionId?: string;
      storageAccount?: string;
      resourceGroup?: string;
    },
  ) => {
    let qs = "/blast/databases/check-updates";
    if (
      options?.subscriptionId &&
      options?.storageAccount &&
      options?.resourceGroup
    ) {
      qs +=
        `?subscription_id=${encodeURIComponent(options.subscriptionId)}` +
        `&storage_account=${encodeURIComponent(options.storageAccount)}` +
        `&resource_group=${encodeURIComponent(options.resourceGroup)}`;
    }
    return api.get<{
      latest_version: string;
      updates_available: Array<{
        db: string;
        snapshot?: string;
        signature_etag?: string;
        stored_etag?: string | null;
        stored_source_version?: string | null;
      }>;
      degraded?: boolean;
      degraded_reason?: string;
      message?: string;
    }>(qs);
  },

  /**
   * Dry-run NCBI snapshot summary for a DB the user might pull. Drives the
   * "version info BEFORE download" surface in BlastDbModal — file count,
   * snapshot id, estimated bytes, last-modified, and a clear "not on S3"
   * hint when the DB exists only on the NCBI FTP mirror.
   */
  previewDatabase: (dbName: string) =>
    api.get<{
      db_name: string;
      snapshot: string;
      available: boolean;
      file_count: number;
      volume_count?: number;
      total_bytes_estimate?: number;
      last_modified?: string | null;
      signature_key?: string | null;
      signature_etag?: string | null;
      files_sample?: string[];
      source?: string;
      message?: string;
    }>(`/blast/databases/${encodeURIComponent(dbName)}/preview`),

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

  buildDbOrderOracle: (
    body: {
      subscription_id: string;
      resource_group: string;
      account_name: string;
      cluster_name: string;
      acr_name?: string;
      image?: string;
      source_version?: string;
    },
    dbName: string,
  ) =>
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

  resultsAggregate: (
    jobId: string,
    subscriptionId: string,
    storageAccount: string,
    resourceGroup?: string,
  ) =>
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
      `/blast/jobs/${encodeURIComponent(jobId)}/results/aggregate?subscription_id=${encodeURIComponent(subscriptionId)}&storage_account=${encodeURIComponent(storageAccount)}${resourceGroup ? `&resource_group=${encodeURIComponent(resourceGroup)}` : ""}`,
    ),

  resultsAlignments: (
    jobId: string,
    subscriptionId: string,
    storageAccount: string,
    resourceGroup?: string,
    opts?: {
      blob_name?: string;
      max_alignments?: number;
      page?: number;
      page_size?: number;
      query_id?: string;
      subject_id?: string;
      organism?: string;
      min_identity?: number;
      min_bitscore?: number;
      max_evalue?: number;
      min_query_cover?: number;
      sort_by?: "relevance" | "evalue" | "bitscore" | "pident" | "qcovs" | "length";
      sort_dir?: "asc" | "desc";
    },
  ) => {
    const params = new URLSearchParams({
      subscription_id: subscriptionId,
      storage_account: storageAccount,
    });
    if (resourceGroup) params.set("resource_group", resourceGroup);
    if (opts?.blob_name) params.set("blob_name", opts.blob_name);
    if (opts?.max_alignments) params.set("max_alignments", String(opts.max_alignments));
    if (opts?.page) params.set("page", String(opts.page));
    if (opts?.page_size) params.set("page_size", String(opts.page_size));
    if (opts?.query_id) params.set("query_id", opts.query_id);
    if (opts?.subject_id) params.set("subject_id", opts.subject_id);
    if (opts?.organism) params.set("organism", opts.organism);
    if (opts?.min_identity !== undefined)
      params.set("min_identity", String(opts.min_identity));
    if (opts?.min_bitscore !== undefined)
      params.set("min_bitscore", String(opts.min_bitscore));
    if (opts?.max_evalue !== undefined) params.set("max_evalue", String(opts.max_evalue));
    if (opts?.min_query_cover !== undefined)
      params.set("min_query_cover", String(opts.min_query_cover));
    if (opts?.sort_by) params.set("sort_by", opts.sort_by);
    if (opts?.sort_dir) params.set("sort_dir", opts.sort_dir);
    return api.get<{
      job_id: string;
      blob_name: string;
      blob_names?: string[];
      alignments: BlastHit[];
      total_hits: number;
      returned: number;
      query_ids: string[];
      subject_aggregates?: BlastSubjectAggregate[];
      page?: number;
      page_size?: number;
      pages?: number;
      files_parsed?: number;
      total_files?: number;
      read_failures?: number;
      truncated?: boolean;
      hit_limit_reached?: boolean;
      degraded?: boolean;
      degraded_reason?: string;
      message?: string;
      filtered_hits?: number;
      filters?: Record<string, unknown>;
      tie_cutoff?: BlastTieCutoff;
    }>(
      `/blast/jobs/${encodeURIComponent(jobId)}/results/alignments?${params.toString()}`,
    );
  },

  /**
   * Server-side organism rollup of the BLAST hits — same filter
   * parameters as `resultsAlignments` so a narrowing applied on the
   * Descriptions tab carries through. Page size does NOT apply; the
   * rollup is always over the filtered (not paginated) set.
   *
   * The frontend `TaxonomyPanel` prefers this endpoint when available
   * and falls back to its page-local rollup when the server returns
   * `degraded: true` or no organisms.
   */
  resultsTaxonomy: (
    jobId: string,
    subscriptionId: string,
    storageAccount: string,
    resourceGroup?: string,
    opts?: {
      blob_name?: string;
      query_id?: string;
      subject_id?: string;
      organism?: string;
      min_identity?: number;
      min_bitscore?: number;
      max_evalue?: number;
      min_query_cover?: number;
      include_lineage?: boolean;
      lineage_taxid_limit?: number;
    },
  ) => {
    const params = new URLSearchParams({
      subscription_id: subscriptionId,
      storage_account: storageAccount,
    });
    if (resourceGroup) params.set("resource_group", resourceGroup);
    if (opts?.blob_name) params.set("blob_name", opts.blob_name);
    if (opts?.query_id) params.set("query_id", opts.query_id);
    if (opts?.subject_id) params.set("subject_id", opts.subject_id);
    if (opts?.organism) params.set("organism", opts.organism);
    if (opts?.min_identity !== undefined)
      params.set("min_identity", String(opts.min_identity));
    if (opts?.min_bitscore !== undefined)
      params.set("min_bitscore", String(opts.min_bitscore));
    if (opts?.max_evalue !== undefined) params.set("max_evalue", String(opts.max_evalue));
    if (opts?.min_query_cover !== undefined)
      params.set("min_query_cover", String(opts.min_query_cover));
    if (opts?.include_lineage) params.set("include_lineage", "true");
    if (opts?.lineage_taxid_limit !== undefined)
      params.set("lineage_taxid_limit", String(opts.lineage_taxid_limit));
    return api.get<{
      job_id: string;
      organisms: BlastTaxonomyRow[];
      total_hits: number;
      filtered_hits?: number;
      files_parsed: number;
      total_files: number;
      read_failures: number;
      truncated?: boolean;
      degraded?: boolean;
      degraded_reason?: string;
      message?: string;
      lineage?: {
        requested: boolean;
        looked_up: number;
        failed: number;
        limit_reached?: number;
      };
    }>(
      `/blast/jobs/${encodeURIComponent(jobId)}/results/taxonomy?${params.toString()}`,
    );
  },

  getCapacityGate: (context: {
    subscriptionId: string;
    resourceGroup: string;
    clusterName: string;
    program?: string;
    database?: string;
  }) => {
    const params = new URLSearchParams({
      subscription_id: context.subscriptionId,
      resource_group: context.resourceGroup,
      cluster_name: context.clusterName,
    });
    if (context.program) params.set("program", context.program);
    if (context.database) params.set("database", context.database);
    return api.get<{
      data: CapacityGateSnapshot;
      meta?: ApiResponseMeta;
    }>(`/blast/capacity?${params.toString()}`);
  },
};

export interface CapacityGateSnapshot {
  enabled: boolean;
  pool: string;
  slots: { in_use: number; max: number };
  cpu_request_pct: number;
  memory_request_pct: number;
  watermark_cpu_pct: number;
  watermark_memory_pct: number;
  pending_pods: number;
  decision_preview: "admit" | "deny";
  decision_reason: string | null;
  decision_retryable: boolean;
  predicted_demand: { cpu_m: number; mem_mib: number };
  active_reservations: Array<{
    job_id: string;
    reserved_at: string;
    cpu_m: number;
    mem_mib: number;
  }>;
  signals_degraded: boolean;
  signals_error: string | null;
  counters?: CapacityGateCounters;
}

export interface CapacityGateCounters {
  admit_total: number;
  deny_total: number;
  release_total: number;
  reserve_lost_total: number;
  deny_by_reason: Record<string, number>;
  last_event_at: string | null;
}

export function capacityGateBandClass(
  snapshot: Pick<
    CapacityGateSnapshot,
    | "enabled"
    | "cpu_request_pct"
    | "memory_request_pct"
    | "watermark_cpu_pct"
    | "watermark_memory_pct"
    | "decision_preview"
    | "signals_degraded"
  >,
): "is-disabled" | "is-degraded" | "is-warning" | "is-danger" | "is-ok" {
  if (!snapshot.enabled) return "is-disabled";
  if (snapshot.signals_degraded) return "is-degraded";
  if (snapshot.decision_preview === "deny") return "is-danger";
  const cpuOver = snapshot.cpu_request_pct >= snapshot.watermark_cpu_pct;
  const memOver = snapshot.memory_request_pct >= snapshot.watermark_memory_pct;
  if (cpuOver || memOver) return "is-warning";
  return "is-ok";
}

export interface BlastSubjectAggregate {
  sseqid: string;
  max_bitscore: number;
  total_bitscore: number;
  hsp_count: number;
  stitle?: string;
  sscinames?: string;
  staxids?: string;
}

/**
 * Score-class truncation signal surfaced from the merge step's
 * merge-report.json. Present on the alignments payload only when the
 * max_target_seqs cutoff split a tied score class (`overflow_count > 0`) or
 * the opt-in diversity-aware cutoff reserved near-miss slots
 * (`diversity_reserved_count > 0`). Absent otherwise.
 */
export interface BlastTieCutoff {
  /** Number of hits in the top tied score class that were dropped because the
   *  displayed set hit the max_target_seqs limit. */
  overflow_count: number;
  /** Slots reserved for lower-scoring near-miss hits by the opt-in
   *  diversity-aware cutoff (0 when that mode is off). */
  diversity_reserved_count: number;
  /** The max_target_seqs value in effect for the job, when recorded. */
  max_target_seqs?: number;
  /** Up to 5 sample queries whose top score class was truncated. */
  queries?: Array<{ query_id?: string; overflow_count?: number }>;
}

export interface BlastTaxonomyRow {
  key: string;
  organism: string;
  taxid: string;
  count: number;
  best_evalue: number | null;
  top_bitscore: number | null;
  /** Raw NCBI Lineage string ("Viruses; Monodnaviria; …"). Present when
   *  the caller requested `include_lineage=true` and the eutils call
   *  succeeded for this taxid. */
  lineage?: string;
  /** Parsed `LineageEx` chain root → leaf. Same source as `lineage`. */
  lineage_ex?: Array<{
    rank: string;
    taxid: number;
    scientific_name: string;
  }>;
  /** NCBI "Blast Name" group (viruses, bacteria, mammals, plants, …)
   *  derived server-side from the lineage chain. */
  blast_name?: string;
  /** How the `organism` field was populated:
   *  - "sscinames" — pulled from the BLAST output column (authoritative).
   *  - "stitle"    — heuristic extraction from the subject title.
   *  Empty when the row was bucketed as `unclassified`. */
  organism_source?: string;
  /** Set to "name_lookup" when the taxid was resolved server-side from
   *  the organism name via NCBI eutils (i.e. the row originated from
   *  the stitle fallback, not from a `staxids` column). */
  taxid_source?: string;
}
