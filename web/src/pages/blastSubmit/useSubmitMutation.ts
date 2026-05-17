import { useMutation } from "@tanstack/react-query";
import type { NavigateFunction } from "react-router-dom";

import { formatApiError } from "@/api/client";
import {
  type AksClusterSummary,
  type BlastSubmitRequest,
  blastApi,
} from "@/api/endpoints";
import {
  hasStructuredTaxidOptionConflict,
  parsePositiveTaxid,
  type FormState,
} from "@/pages/blastSubmitModel";

import {
  getWorkloadNodeCount,
  getWorkloadNodeSku,
} from "./computeEnvironment";
import type { ToastFn } from "./types";

export interface UseSubmitMutationArgs {
  navigate: NavigateFunction;
  toast: ToastFn;
  clearDraft: () => void;
}

export function useSubmitMutation({
  navigate,
  toast,
  clearDraft,
}: UseSubmitMutationArgs) {
  return useMutation({
    mutationFn: (req: BlastSubmitRequest) => blastApi.submit(req),
    onSuccess: (resp) => {
      clearDraft();
      toast("BLAST search submitted! Tracking your job…", "success");
      const jobId = resp?.job_id || resp?.id || resp?.instance_id;
      if (jobId) navigate(`/blast/jobs/${encodeURIComponent(jobId)}`);
      else navigate("/blast/jobs");
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
  let opts = form.additional_options || "";
  if (
    form.low_complexity_filter &&
    form.program === "blastn" &&
    !opts.includes("-dust")
  )
    opts += " -dust yes";
  if (form.query_from && form.query_to)
    opts += ` -query_loc ${form.query_from}-${form.query_to}`;
  if (form.match_score) opts += ` -reward ${form.match_score}`;
  if (form.mismatch_score) opts += ` -penalty ${form.mismatch_score}`;

  const dbShort = form.db.split("/").pop() || form.db;
  const autoTitle = form.job_title || `${form.program} · ${dbShort}`;
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

  return {
    subscription_id: subId,
    resource_group: workloadRg,
    region: selectedCluster.region || region,
    program: form.program,
    db: form.db,
    query_data: form.query_data || undefined,
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
    additional_options: opts.trim() || undefined,
    machine_type: workloadNodeSku || "Standard_E32s_v5",
    num_nodes: workloadNodeCount || 3,
    pd_size: "1000Gi",
    mem_request: "8Gi",
    mem_limit: "24Gi",
    enable_warmup: form.enable_warmup,
    use_local_ssd: true,
    db_auto_partition: form.sharding_mode !== "off",
    sharding_mode: form.sharding_mode,
    db_effective_search_space: dbEffectiveSearchSpace,
    db_total_letters: dbTotalLetters,
    db_total_bytes: dbTotalBytes,
    shard_sets: dbShardSets && dbShardSets.length > 0 ? dbShardSets : undefined,
    allow_approximate_sharding:
      form.sharding_mode === "approximate" || undefined,
    disable_sharding: form.disable_sharding,
    acr_resource_group: acrRg || undefined,
    acr_name: acrName || undefined,
    storage_account: storageAccount,
    aks_cluster_name: selectedCluster.name,
  };
}
