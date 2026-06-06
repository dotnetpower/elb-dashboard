/**
 * blast.types — request/response types for the BLAST API surface.
 *
 * Extracted from `blast.ts` (issue #24) so the type declarations and the
 * runtime API client live in separate modules. `blast.ts` re-exports every
 * symbol here, so existing `import { Foo } from "@/api/blast"` consumers keep
 * working unchanged.
 *
 * This file is types-only — no runtime values, no imports with side effects.
 */

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

/**
 * One row of the Sequence Detail "Your BLAST jobs for this accession" card.
 * Whitelisted projection returned by `GET /blast/jobs/by-accession/{accession}`
 * — no raw payload, no Storage URLs.
 */
export interface BlastJobForAccession {
  job_id: string;
  status: string;
  phase: string;
  database: string;
  created_at: string | null;
  query_accession: string | null;
  /** 1-based inclusive sub-range start, or null for a whole-sequence job. */
  seq_start: number | null;
  /** 1-based inclusive sub-range stop, or null for a whole-sequence job. */
  seq_stop: number | null;
  /** SPA route to the job detail page, e.g. `/blast/jobs/<job_id>`. */
  detail_url: string;
}

export interface JobsForAccessionResponse {
  accession: string;
  accession_base: string;
  match: "base" | "exact";
  count: number;
  jobs: BlastJobForAccession[];
  /** True when the jobstate lookup failed; `jobs` is empty and the card degrades calmly. */
  degraded: boolean;
  reason: string | null;
  meta?: ApiResponseMeta;
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
  /**
   * BLASTDB `bytes-to-cache` — the database's in-memory footprint that
   * ElasticBLAST compares against the workload node's RAM during a full-DB
   * (non-sharded) submit. When it exceeds node RAM, ElasticBLAST's pre-flight
   * rejects the run ("memory requirements exceed memory available..."). The
   * submit form uses it to block a full-DB run that cannot fit. Optional —
   * absent for DBs whose `.njs` metadata has not been read.
   */
  bytes_to_cache?: number;
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
    /** AKS-fanout only: total expected bytes (drives the byte-based ETA). */
    bytes_total?: number;
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

/** One database suggestion (recommended or alternative) from the oracle. */
export interface BlastDbSuggestion {
  db: string;
  label: string;
  rationale: string;
}

/** Response of GET /api/blast/databases/recommend (R8 selection oracle). */
export interface BlastDbRecommendation {
  ruleset_version: string;
  molecule: "dna" | "protein";
  goal: string;
  program: string;
  taxon?: string | null;
  recommended: BlastDbSuggestion;
  alternative: BlastDbSuggestion;
  notes: string[];
}

/** Search goals accepted by the recommendation oracle (mirrors SUPPORTED_GOALS). */
export type BlastRecommendGoal =
  | "identify"
  | "highly_similar"
  | "transcripts"
  | "genomes"
  | "well_characterized"
  | "comprehensive";
