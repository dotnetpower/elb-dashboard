import type { AksClusterSummary, BlastDatabase } from "@/api/endpoints";
import type { FormState } from "@/pages/blastSubmitModel";
import {
  getWorkloadNodeCount,
  getWorkloadNodeSku,
} from "@/pages/blastSubmit/computeEnvironment";
import { type ShardCapacityPlan, planPartitionsForSubmit } from "@/utils/dbSharding";

export type ShardingMode = FormState["sharding_mode"];

export interface ShardingModeAvailability {
  mode: ShardingMode;
  label: string;
  enabled: boolean;
  reason: string | null;
  description: string;
}

export interface ShardingAvailability {
  capacityPlan: ShardCapacityPlan | null;
  options: Record<ShardingMode, ShardingModeAvailability>;
  preferredMode: ShardingMode;
}

export interface DeriveShardingAvailabilityArgs {
  cluster?: AksClusterSummary;
  database?: BlastDatabase;
  isDbAlreadyWarm: boolean;
  /**
   * Whether the warmup-status query has produced a trustworthy answer for the
   * selected cluster yet. While it is still disabled (cluster not workload-
   * ready) or loading, `isDbAlreadyWarm` is `false` even for a DB that is
   * actually warm, so the gate must show a neutral "checking" message instead
   * of telling the user to warm an already-warm database. Defaults to `true`
   * to preserve the legacy behaviour for callers that do not pass it.
   */
  isWarmupStatusResolved?: boolean;
  outfmt: number;
}

export interface ReconcileShardingSelectionArgs {
  form: FormState;
  availability: ShardingAvailability;
  isDbAlreadyWarm: boolean;
  autoWarmupSelected?: boolean;
}

function isMergeCompatibleOutfmt(outfmt: number): boolean {
  return outfmt === 5 || outfmt === 6;
}

export function hasPreparedShardLayout(database?: BlastDatabase): boolean {
  return Boolean(
    database?.name === "core_nt" &&
    database.sharded === true &&
    database.shard_sets?.some((shardCount) => shardCount > 1),
  );
}

function unavailableReason({
  cluster,
  database,
  isDbAlreadyWarm,
  isWarmupStatusResolved = true,
  outfmt,
  capacityPlan,
}: DeriveShardingAvailabilityArgs & {
  capacityPlan: ShardCapacityPlan | null;
}): string | null {
  if (!database) return "Select a database first.";
  if (!cluster) return "Select a cluster first.";
  if (!isDbAlreadyWarm) {
    // Distinguish "we have not heard back yet" from "confirmed cold". Telling a
    // user to warm a database that is already warm (but whose status query is
    // still loading/disabled) is the warmup-status conflation bug.
    if (!isWarmupStatusResolved)
      return "Checking warm status on the selected cluster\u2026";
    return "Warm this database on the selected cluster before using sharded performance modes.";
  }
  if (database.sharding_in_progress)
    return "Shard preparation is still running for this database.";
  if (database.sharding_error)
    return `Shard preparation failed: ${database.sharding_error}`;
  if (!hasPreparedShardLayout(database)) {
    return "Prepared shard layouts are not available for this database yet.";
  }
  if (!database.total_bytes || database.total_bytes <= 0) {
    return "Database size metadata is missing, so shard capacity cannot be checked.";
  }
  if (!isMergeCompatibleOutfmt(outfmt)) {
    return "Sharded result merging supports output format 5 or 6 only.";
  }
  if (!capacityPlan?.feasible)
    return (
      capacityPlan?.reason ??
      "This database is too large for the selected cluster shard layout."
    );
  return null;
}

export function deriveShardingAvailability({
  cluster,
  database,
  isDbAlreadyWarm,
  isWarmupStatusResolved = true,
  outfmt,
}: DeriveShardingAvailabilityArgs): ShardingAvailability {
  const nodeCount = cluster ? getWorkloadNodeCount(cluster) : null;
  const nodeSku = cluster ? getWorkloadNodeSku(cluster) : null;
  const capacityPlan =
    database?.total_bytes && nodeCount && nodeSku && hasPreparedShardLayout(database)
      ? planPartitionsForSubmit(
          database.total_bytes,
          nodeCount,
          nodeSku,
          database.shard_sets,
        )
      : null;
  const reason = unavailableReason({
    cluster,
    database,
    isDbAlreadyWarm,
    isWarmupStatusResolved,
    outfmt,
    capacityPlan,
  });
  const enabled = reason == null;
  const hasVerifiedWebSearchSpace =
    typeof database?.web_blast_searchsp === "number" && database.web_blast_searchsp > 0;
  const preciseReason =
    reason ??
    (hasVerifiedWebSearchSpace
      ? null
      : "Verified Web BLAST search-space evidence is not available for this database/options scope.");
  return {
    capacityPlan,
    preferredMode: preciseReason == null ? "precise" : enabled ? "approximate" : "off",
    options: {
      off: {
        mode: "off",
        label: "Off",
        enabled: true,
        reason: null,
        description:
          "Run against the selected database without partitioned shards. This is the safest baseline and keeps full-DB BLAST semantics, but large databases start slower.",
      },
      approximate: {
        mode: "approximate",
        label: "Fast shard",
        enabled,
        reason,
        description:
          "Use prepared node-local DB shards and merge top hits. This is a throughput probe mode; full Web BLAST equivalence is not claimed.",
      },
      precise: {
        mode: "precise",
        label: hasVerifiedWebSearchSpace ? "Web-equivalent shard" : "Precise shard",
        enabled: preciseReason == null,
        reason: preciseReason,
        description: hasVerifiedWebSearchSpace
          ? "Use warmed shards with verified full-DB search-space correction and query-aware merge checks. This is the default path for evidence-backed NCBI Web BLAST-compatible runs."
          : "Use warmed shards only after a verified full-DB search-space default or explicit calibration evidence is available for this database/options scope.",
      },
    },
  };
}

export function reconcileShardingSelection({
  form,
  availability,
  isDbAlreadyWarm,
  autoWarmupSelected = false,
}: ReconcileShardingSelectionArgs): FormState {
  const selectedMode = availability.options[form.sharding_mode];
  const shouldFallbackFromUnavailable =
    form.sharding_mode !== "off" && !selectedMode.enabled;
  const shouldPreferShardedMode =
    form.sharding_mode === "off" &&
    (!form.disable_sharding || autoWarmupSelected) &&
    availability.preferredMode !== "off" &&
    availability.options[availability.preferredMode].enabled;
  const nextShardingMode =
    shouldFallbackFromUnavailable || shouldPreferShardedMode
      ? availability.preferredMode
      : form.sharding_mode;
  const nextEnableWarmup = form.enable_warmup || isDbAlreadyWarm;
  const nextDbAutoPartition = nextShardingMode !== "off";
  const nextDisableSharding =
    nextShardingMode === "off" ? form.disable_sharding : false;

  if (
    form.enable_warmup === nextEnableWarmup &&
    form.sharding_mode === nextShardingMode &&
    form.db_auto_partition === nextDbAutoPartition &&
    form.disable_sharding === nextDisableSharding
  ) {
    return form;
  }

  return {
    ...form,
    enable_warmup: nextEnableWarmup,
    sharding_mode: nextShardingMode,
    db_auto_partition: nextDbAutoPartition,
    disable_sharding: nextDisableSharding,
  };
}
