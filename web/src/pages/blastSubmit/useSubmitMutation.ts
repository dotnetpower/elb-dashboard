import { useMutation } from "@tanstack/react-query";
import type { NavigateFunction } from "react-router-dom";

import { formatApiError } from "@/api/client";
import {
  type AksClusterSummary,
  type BlastSubmitRequest,
  blastApi,
} from "@/api/endpoints";
import {
  buildGeneratedJobTitle,
  hasCliFlag,
  hasStructuredTaxidOptionConflict,
  parsePositiveTaxid,
  shouldUseBlastnShortTask,
  type FormState,
} from "@/pages/blastSubmitModel";

import { getWorkloadNodeCount, getWorkloadNodeSku } from "./computeEnvironment";
import type { ToastFn } from "./types";

function hasPreparedShardLayoutForSubmit(db: string, shardSets?: number[]): boolean {
  const dbName = db.split("/").filter(Boolean).pop() || db;
  return dbName === "core_nt" && Boolean(shardSets?.some((shardCount) => shardCount > 1));
}

function appendOption(options: string, flag: string, value?: string): string {
  if (hasCliFlag(options, flag)) return options;
  return `${options} ${flag}${value ? ` ${value}` : ""}`.trim();
}

export function buildEffectiveAdditionalOptions(form: FormState): string | undefined {
  let opts = form.additional_options || "";
  if (shouldUseBlastnShortTask(form)) opts = appendOption(opts, "-task", "blastn-short");
  if (form.low_complexity_filter && form.program === "blastn")
    opts = appendOption(opts, "-dust", "yes");
  if (form.query_from && form.query_to)
    opts = appendOption(opts, "-query_loc", `${form.query_from}-${form.query_to}`);
  if (form.match_score) opts = appendOption(opts, "-reward", form.match_score);
  if (form.mismatch_score) opts = appendOption(opts, "-penalty", form.mismatch_score);
  if (form.max_matches_in_query_range && form.max_matches_in_query_range !== "0") {
    opts = appendOption(opts, "-culling_limit", form.max_matches_in_query_range);
  }
  if (form.mask_lookup_table_only) opts = appendOption(opts, "-soft_masking", "true");
  else if (form.low_complexity_filter && form.program === "blastn") {
    opts = appendOption(opts, "-soft_masking", "false");
  }
  if (form.mask_lowercase) opts = appendOption(opts, "-lcase_masking");
  if (form.species_repeat_filter && form.repeat_filter_taxid.trim()) {
    opts = appendOption(opts, "-window_masker_taxid", form.repeat_filter_taxid.trim());
  }
  return opts.trim() || undefined;
}

export interface UseSubmitMutationArgs {
  navigate: NavigateFunction;
  toast: ToastFn;
  clearDraft: () => void;
}

export function buildSubmittedJobUrl(
  resp: { job_id?: string; id?: string; instance_id?: string } | undefined,
  req: Pick<
    BlastSubmitRequest,
    "subscription_id" | "resource_group" | "storage_account" | "aks_cluster_name"
  >,
): string {
  const jobId = resp?.job_id || resp?.id || resp?.instance_id;
  if (!jobId) return "/blast/jobs";

  const params = new URLSearchParams();
  params.set("submitted", "1");
  if (req.subscription_id) params.set("subscription_id", req.subscription_id);
  if (req.resource_group) params.set("resource_group", req.resource_group);
  if (req.storage_account) params.set("storage_account", req.storage_account);
  if (req.aks_cluster_name) params.set("cluster_name", req.aks_cluster_name);
  const query = params.toString();
  return `/blast/jobs/${encodeURIComponent(jobId)}${query ? `?${query}` : ""}`;
}

export function useSubmitMutation({
  navigate,
  toast,
  clearDraft,
}: UseSubmitMutationArgs) {
  return useMutation({
    mutationFn: (req: BlastSubmitRequest) => blastApi.submit(req),
    onSuccess: (resp, req) => {
      clearDraft();
      toast("BLAST search submitted! Tracking your job…", "success");
      navigate(buildSubmittedJobUrl(resp, req));
    },
    onError: (err: Error) => {
      const friendly = formatApiError(err, "blast");
      toast(`Submission failed: ${friendly}`, "error");
    },
  });
}

export interface BuildSubmitRequestArgs {
  form: FormState;
  selectedCluster: AksClusterSummary;
  subId: string;
  workloadRg: string;
  storageAccount: string;
  acrRg: string;
  acrName: string;
  region: string;
  dbTotalLetters?: number;
  dbTotalBytes?: number;
  dbEffectiveSearchSpace?: number;
  dbShardSets?: number[];
}

/**
 * Pure assembler — turns the form + selected cluster into a fully-resolved
 * BlastSubmitRequest. Kept separate from the mutation so the test surface
 * is small.
 */
export function buildSubmitRequest({
  form,
  selectedCluster,
  subId,
  workloadRg,
  storageAccount,
  acrRg,
  acrName,
  region,
  dbTotalLetters,
  dbTotalBytes,
  dbEffectiveSearchSpace,
  dbShardSets,
}: BuildSubmitRequestArgs): BlastSubmitRequest {
  const workloadNodeSku = getWorkloadNodeSku(selectedCluster);
  const workloadNodeCount = getWorkloadNodeCount(selectedCluster);

  const dbShort = form.db.split("/").pop() || form.db;
  const autoTitle =
    form.job_title.trim() || buildGeneratedJobTitle(`${form.program} · ${dbShort}`);
  const hasTaxid = form.taxid.trim().length > 0;
  const taxid = hasTaxid ? parsePositiveTaxid(form.taxid) : null;
  if (hasTaxid && taxid === null) {
    throw new Error("Taxonomy taxid must be a positive integer.");
  }
  if (taxid && hasStructuredTaxidOptionConflict(form.additional_options)) {
    throw new Error(
      "Remove -taxids or -negative_taxids from Additional options before using the Taxonomy Filter.",
    );
  }

  const hasPreparedShardLayout = hasPreparedShardLayoutForSubmit(form.db, dbShardSets);
  const effectiveShardingMode =
    hasPreparedShardLayout && !form.disable_sharding ? form.sharding_mode : "off";
  const useSharding = effectiveShardingMode !== "off";

  // Accession is forwarded only when no inline FASTA is present. When set,
  // the backend resolves it via NCBI E-utilities and stages the result like
  // any inline query. `query_from` / `query_to` map to the efetch subrange so
  // we do NOT also let `-query_loc` get applied to the resolved FASTA.
  const inlineQuery = form.query_data.trim();
  const accession = form.query_accession.trim();
  const useAccession = !inlineQuery && accession.length > 0;
  const parseSubrange = (value: string): number | undefined => {
    const trimmed = value.trim();
    if (!trimmed) return undefined;
    const parsed = Number.parseInt(trimmed, 10);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : undefined;
  };

  // When the query came from an accession the subrange travels in
  // `query_accession_seq_start/stop`, so suppress the form-driven `-query_loc`
  // that `buildEffectiveAdditionalOptions` would otherwise append.
  const formForOptions = useAccession ? { ...form, query_from: "", query_to: "" } : form;
  const opts = buildEffectiveAdditionalOptions(formForOptions);

  return {
    subscription_id: subId,
    resource_group: workloadRg,
    region: selectedCluster.region || region,
    program: form.program,
    db: form.db,
    query_data: form.query_data || undefined,
    query_accession: useAccession ? accession : undefined,
    query_accession_seq_start: useAccession ? parseSubrange(form.query_from) : undefined,
    query_accession_seq_stop: useAccession ? parseSubrange(form.query_to) : undefined,
    job_title: autoTitle,
    evalue: form.evalue,
    max_target_seqs: form.max_target_seqs,
    outfmt: form.outfmt,
    word_size: form.word_size ? parseInt(form.word_size, 10) : undefined,
    gap_open: form.gap_open ? parseInt(form.gap_open, 10) : undefined,
    gap_extend: form.gap_extend ? parseInt(form.gap_extend, 10) : undefined,
    low_complexity_filter: form.low_complexity_filter,
    taxid: taxid ?? undefined,
    is_inclusive: taxid ? form.is_inclusive : undefined,
    additional_options: opts,
    machine_type: workloadNodeSku || "Standard_E32s_v5",
    num_nodes: workloadNodeCount || 3,
    pd_size: "1000Gi",
    mem_request: "8Gi",
    mem_limit: "24Gi",
    enable_warmup: form.enable_warmup,
    use_local_ssd: true,
    db_auto_partition: useSharding,
    sharding_mode: effectiveShardingMode,
    use_db_order_oracle: effectiveShardingMode === "precise" || undefined,
    db_effective_search_space: dbEffectiveSearchSpace,
    db_total_letters: dbTotalLetters,
    db_total_bytes: dbTotalBytes,
    shard_sets: useSharding ? dbShardSets : undefined,
    allow_approximate_sharding: effectiveShardingMode === "approximate" || undefined,
    disable_sharding: !useSharding,
    acr_resource_group: acrRg || undefined,
    acr_name: acrName || undefined,
    storage_account: storageAccount,
    aks_cluster_name: selectedCluster.name,
  };
}
