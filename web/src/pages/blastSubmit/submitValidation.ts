import type { BlastDatabase, AksClusterSummary } from "@/api/endpoints";
import type { BlastWarmupPlan } from "@/api/blast";
import {
  hasStructuredTaxidOptionConflict,
  parsePositiveTaxid,
  type FormState,
} from "@/pages/blastSubmitModel";
import { isAksWorkloadReady } from "@/utils/aksStatus";
import {
  blastDbBlockedReason,
  getBlastDbReadiness,
} from "@/utils/blastDbReady";

import {
  databaseExists,
  getDatabaseWarning,
  getDbBaseName,
  getSequenceStats,
} from "./helpers";
import type { ProgramMeta } from "./types";

export interface MissingItem {
  text: string;
  link?: string;
}

export interface SubmitValidationArgs {
  form: FormState;
  programMeta: ProgramMeta;
  subId: string;
  workloadRg: string;
  storageAccount: string;
  selectedCluster: AksClusterSummary | undefined;
  dbQueryData: { databases?: BlastDatabase[] } | undefined;
  dbQueryIsSuccess: boolean;
  warmupBlocked: boolean;
  selectedDbPlan: BlastWarmupPlan | null | undefined;
  shardingBlockedReason?: string | null;
  dataLoading?: boolean;
  submitPending: boolean;
}

export interface SubmitValidationResult {
  knownDbs: BlastDatabase[];
  dbListResolved: boolean;
  dbBaseName: string;
  dbMissingFromStorage: boolean;
  /** True when the selected DB exists in Storage but is not yet usable
   *  (copy_status.phase !== "completed", update_in_progress, etc.). */
  dbNotReady: boolean;
  /** Human reason for `dbNotReady` (mirrors `blastDbBlockedReason`). */
  dbNotReadyReason: string | null;
  dbWarning: string | null;
  canSubmit: boolean;
  missing: MissingItem[];
  paramsSummary: string;
  searchSummary: string;
  seqStats: ReturnType<typeof getSequenceStats>;
  readySteps: { ok: boolean; label: string }[];
  readyCount: number;
}

export function deriveSubmitValidation({
  form,
  programMeta,
  subId,
  workloadRg,
  storageAccount,
  selectedCluster,
  dbQueryData,
  dbQueryIsSuccess,
  warmupBlocked,
  selectedDbPlan,
  shardingBlockedReason,
  dataLoading = false,
  submitPending,
}: SubmitValidationArgs): SubmitValidationResult {
  const knownDbs = dbQueryData?.databases ?? [];
  const dbListResolved = dbQueryIsSuccess && knownDbs.length > 0;
  const dbBaseName = getDbBaseName(form.db);
  const selectedDb = knownDbs.find((database) => database.name === dbBaseName);
  const dbMissingFromStorage =
    Boolean(form.db) && dbListResolved && !databaseExists(knownDbs, form.db);
  // The DB is in the listing but not yet usable (mid-copy / mid-update). This
  // is distinct from "missing" and gets a different remediation message.
  const readiness = selectedDb ? getBlastDbReadiness(selectedDb) : null;
  const dbNotReady =
    Boolean(form.db) && !dbMissingFromStorage && readiness != null && !readiness.ready;
  const dbNotReadyReason = readiness && !readiness.ready ? blastDbBlockedReason(readiness) : null;
  // A query can be supplied either as inline FASTA (`query_data`) or as an
  // NCBI accession (`query_accession`) that the backend resolves to FASTA at
  // submit time. Either source satisfies the "Sequence" readiness step.
  const hasInlineQuery = form.query_data.trim().length > 0;
  const hasAccession = form.query_accession.trim().length > 0;
  const hasQuery = hasInlineQuery || hasAccession;
  const hasTaxid = form.taxid.trim().length > 0;
  const taxidValid = !hasTaxid || parsePositiveTaxid(form.taxid) !== null;
  const taxidOptionConflict =
    hasTaxid && hasStructuredTaxidOptionConflict(form.additional_options);
  const taxonomyReady = !hasTaxid || (taxidValid && !taxidOptionConflict);

  const canSubmit = Boolean(
    subId &&
    workloadRg &&
    form.program &&
    form.db &&
    hasQuery &&
    storageAccount &&
    selectedCluster &&
    isAksWorkloadReady(selectedCluster) &&
    !dbMissingFromStorage &&
    !dbNotReady &&
    !warmupBlocked &&
    !shardingBlockedReason &&
    !dataLoading &&
    taxonomyReady &&
    !submitPending,
  );

  const missing: MissingItem[] = [];
  if (!subId || !workloadRg)
    missing.push({ text: "Azure resources not configured", link: "/" });
  if (!hasQuery) missing.push({ text: "Query sequence or NCBI accession" });
  else if (hasInlineQuery && !form.query_data.trim().startsWith(">"))
    missing.push({ text: "Query must be in FASTA format (start with '>')" });
  if (!form.db) missing.push({ text: "Database" });
  else if (dbMissingFromStorage)
    missing.push({
      text: `Database '${form.db.split("/").pop()}' is not in storage — download it from the Dashboard first`,
      link: "/",
    });
  else if (dbNotReady)
    missing.push({
      text:
        dbNotReadyReason ??
        `Database '${form.db.split("/").pop()}' is not ready yet — wait for the download to complete`,
      link: "/",
    });
  if (!storageAccount) missing.push({ text: "Storage account", link: "/" });
  if (!selectedCluster)
    missing.push({
      text: "AKS cluster — create one on the Dashboard",
      link: "/",
    });
  else if (!isAksWorkloadReady(selectedCluster))
    missing.push({ text: "AKS cluster must be fully provisioned and running" });
  if (warmupBlocked)
    missing.push({
      text:
        selectedDbPlan?.message ??
        "Warmup is not feasible on this cluster — disable warmup or upgrade the cluster",
    });
  if (shardingBlockedReason) missing.push({ text: shardingBlockedReason });
  if (dataLoading) missing.push({ text: "Runtime data is still loading" });
  if (!taxidValid) missing.push({ text: "Taxonomy taxid must be a positive integer" });
  if (taxidOptionConflict) {
    missing.push({
      text: "Remove -taxids or -negative_taxids from Additional options before using the Taxonomy Filter",
    });
  }

  const dbWarning = getDatabaseWarning(form, programMeta);
  const searchspSummary = selectedDb?.web_blast_searchsp
    ? ` · Searchsp: ${selectedDb.web_blast_searchsp}`
    : "";
  const paramsSummary = `E-value: ${form.evalue} · Max: ${form.max_target_seqs} · Fmt: ${form.outfmt}${searchspSummary}`;
  const searchSummary = form.db
    ? `Search ${form.db.split("/").pop() || form.db} using ${programMeta.label}`
    : "";
  const seqStats = getSequenceStats(form.query_data);

  const readySteps = [
    { ok: Boolean(subId && workloadRg), label: "Config" },
    { ok: hasAccession || Boolean(form.query_data && seqStats.isFasta), label: "Sequence" },
    { ok: Boolean(form.db) && !dbMissingFromStorage && !dbNotReady, label: "Database" },
    { ok: taxonomyReady, label: "Taxonomy" },
    {
      ok: Boolean(selectedCluster && isAksWorkloadReady(selectedCluster)),
      label: "Cluster",
    },
  ];
  const readyCount = readySteps.filter((s) => s.ok).length;

  return {
    knownDbs,
    dbListResolved,
    dbBaseName,
    dbMissingFromStorage,
    dbNotReady,
    dbNotReadyReason,
    dbWarning,
    canSubmit,
    missing,
    paramsSummary,
    searchSummary,
    seqStats,
    readySteps,
    readyCount,
  };
}
