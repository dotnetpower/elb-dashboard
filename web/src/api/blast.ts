import { api, fetchApiRaw } from "@/api/client";
import type { OrchestrationStatus } from "@/api/shared";

import type {
  ApiAdmission,
  ApiResponseMeta,
  BlastAggregateStats,
  BlastCitation,
  BlastCitationFormat,
  BlastCompatibilityContract,
  BlastDatabase,
  BlastDbRecommendation,
  BlastExecutionStepsSnapshot,
  BlastExportFormat,
  BlastHit,
  BlastJobEvent,
  BlastJobSummary,
  BlastJobsListResponse,
  BlastPrecisionReport,
  BlastRecommendGoal,
  BlastResultFile,
  BlastResultManifest,
  BlastSubjectAggregate,
  BlastSubmitRequest,
  BlastSubmitResponse,
  BlastTaxonomyRow,
  BlastTieCutoff,
  CapacityGateSnapshot,
  JobsForAccessionResponse,
  TaxonomyDetail,
  WorkflowExportFormat,
  TaxonomyImageResponse,
  TaxonomySearchResponse,
  TaxonomyTreeResponse,
} from "@/api/blast.types";

// Back-compat: re-export every BLAST type so existing
// `import { Foo } from "@/api/blast"` consumers keep working.
export type * from "@/api/blast.types";

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
    db_total_sequences?: number;
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
    limit?: number;
  }) => {
    const params = new URLSearchParams();
    if (context?.subscriptionId) params.set("subscription_id", context.subscriptionId);
    if (context?.resourceGroup) params.set("resource_group", context.resourceGroup);
    if (context?.clusterName) params.set("cluster_name", context.clusterName);
    if (context?.limit != null) params.set("limit", String(context.limit));
    const qs = params.toString();
    return api.get<BlastJobsListResponse>(`/blast/jobs${qs ? `?${qs}` : ""}`);
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

  /**
   * List the caller's accession-mode BLAST jobs that used `accession` as the
   * query. Owner-scoped on the server; powers the Sequence Detail
   * "Your BLAST jobs for this accession" card. Never throws on a jobstate
   * outage — the server returns `{ degraded: true }` with an empty `jobs`.
   */
  getJobsForAccession: (
    accession: string,
    opts?: { match?: "base" | "exact"; limit?: number },
  ) => {
    const params = new URLSearchParams();
    if (opts?.match) params.set("match", opts.match);
    if (opts?.limit != null) params.set("limit", String(opts.limit));
    const qs = params.toString();
    return api.get<JobsForAccessionResponse>(
      `/blast/jobs/by-accession/${encodeURIComponent(accession)}${qs ? `?${qs}` : ""}`,
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
    onProgress?: (received: number, total: number | null) => void,
  ) => {
    const response = await fetchApiRaw(
      `/blast/jobs/${encodeURIComponent(jobId)}/results/${encodeURIComponent(fileId)}?subscription_id=${encodeURIComponent(subscriptionId)}&storage_account=${encodeURIComponent(storageAccount)}${resourceGroup ? `&resource_group=${encodeURIComponent(resourceGroup)}` : ""}`,
    );
    if (!response.ok) {
      const text = await response.text();
      throw new Error(text || `HTTP ${response.status}`);
    }
    const filename = filenameFromDisposition(
      response.headers.get("Content-Disposition"),
    );
    const contentType =
      response.headers.get("Content-Type") ?? "application/octet-stream";
    const totalHeader = response.headers.get("Content-Length");
    const total = totalHeader ? Number(totalHeader) : null;
    // Stream the body so the caller can render real download progress. The
    // blob is still fully materialised (same behaviour as before); we just
    // observe the bytes as they arrive. Falls back to a one-shot blob read
    // when the body stream is unavailable (older runtimes / no onProgress).
    if (onProgress && response.body) {
      const reader = response.body.getReader();
      const chunks: Uint8Array[] = [];
      let received = 0;
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        if (value) {
          chunks.push(value);
          received += value.length;
          onProgress(received, Number.isFinite(total) ? total : null);
        }
      }
      return {
        blob: new Blob(chunks as BlobPart[], { type: contentType }),
        filename,
        contentType,
      };
    }
    return {
      blob: await response.blob(),
      filename,
      contentType,
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

  /**
   * Download a workflow-manager module (Nextflow / Snakemake / CWL / WDL) that
   * re-submits this job's exact parameters via one POST /api/blast/jobs call
   * (#57 R3). The route streams the file body as text/plain; the caller turns
   * it into a browser download. No storage URL or bearer token is embedded.
   */
  getWorkflowExport: (jobId: string, format: WorkflowExportFormat) =>
    api.getText(
      `/blast/jobs/${encodeURIComponent(jobId)}/export?format=${encodeURIComponent(format)}`,
    ),

  /**
   * Recommend one NCBI database plus an alternative for a described search
   * (R8 selection oracle). Pure decision logic on the backend — no Azure
   * data-plane calls — so this is safe to call eagerly from the submit form.
   */
  getDatabaseRecommendation: (params: {
    program?: string;
    molecule?: "dna" | "protein";
    goal?: BlastRecommendGoal;
    taxon?: string;
  }) => {
    const qs = new URLSearchParams();
    if (params.program) qs.set("program", params.program);
    if (params.molecule) qs.set("molecule", params.molecule);
    if (params.goal) qs.set("goal", params.goal);
    if (params.taxon) qs.set("taxon", params.taxon);
    const query = qs.toString();
    return api.get<BlastDbRecommendation>(
      `/blast/databases/recommend${query ? `?${query}` : ""}`,
    );
  },

  listDatabases: (
    subscriptionId: string,
    storageAccount: string,
    resourceGroup: string,
    clusterTopology?: { numNodes: number; machineType: string },
    options?: { fresh?: boolean },
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
    if (options?.fresh) {
      // Bypass the backend catalogue cache and re-enumerate Storage. Used by
      // the Database Builder "Refresh" affordance so an out-of-band change is
      // reflected immediately instead of waiting out the cache TTL.
      qs += `&fresh=1`;
    }
    return api.get<{
      databases: BlastDatabase[];
      public_access_disabled?: boolean;
      message?: string;
    }>(qs);
  },

  checkUpdates: (options?: {
    subscriptionId?: string;
    storageAccount?: string;
    resourceGroup?: string;
  }) => {
    let qs = "/blast/databases/check-updates";
    if (options?.subscriptionId && options?.storageAccount && options?.resourceGroup) {
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
        composite_signature?: string | null;
        stored_etag?: string | null;
        stored_composite_signature?: string | null;
        stored_source_version?: string | null;
      }>;
      /**
       * True only when the backend actually ran the per-DB NCBI signature
       * comparison (storage scope supplied AND the downloaded-DB list
       * resolved). When true, an empty `updates_available` is authoritative —
       * the SPA must NOT fall back to the coarse `source_version !==
       * latest_version` heuristic, which re-flags every DB whose `latest-dir`
       * merely rotated. When false/undefined the SPA may use that fallback.
       */
      updates_available_evaluated?: boolean;
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
    }>(`/blast/jobs/${encodeURIComponent(jobId)}/results/taxonomy?${params.toString()}`);
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
