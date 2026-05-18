import type { AksClusterSummary, BlastDatabase } from "@/api/endpoints";
import type { FormState } from "@/pages/blastSubmitModel";
import {
  getWorkloadNodeCount,
  getWorkloadNodeSku,
} from "@/pages/blastSubmit/computeEnvironment";
import {
  type ShardCapacityPlan,
  planPartitionsForSubmit,
} from "@/utils/dbSharding";

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
  outfmt: number;
}

function isMergeCompatibleOutfmt(outfmt: number): boolean {
  return outfmt === 5 || outfmt === 6;
}

function unavailableReason({
  cluster,
  database,
  isDbAlreadyWarm,
  outfmt,
  capacityPlan,
}: DeriveShardingAvailabilityArgs & {
  capacityPlan: ShardCapacityPlan | null;
}): string | null {
  if (!database) return "Select a database first.";
  if (!cluster) return "Select a cluster first.";
  if (!isDbAlreadyWarm) return "Warm this database on the selected cluster before using sharded performance modes.";
  if (database.sharding_in_progress) return "Shard preparation is still running for this database.";
  if (database.sharding_error) return `Shard preparation failed: ${database.sharding_error}`;
  if (database.sharded !== true || !database.shard_sets?.length) {
    return "Prepared shard layouts are not available for this database yet.";
  }
  if (!database.total_bytes || database.total_bytes <= 0) {
    return "Database size metadata is missing, so shard capacity cannot be checked.";
  }
  if (!isMergeCompatibleOutfmt(outfmt)) {
    return "Sharded result merging supports output format 5 or 6 only.";
  }
  if (!capacityPlan?.feasible) return capacityPlan?.reason ?? "This database is too large for the selected cluster shard layout.";
  return null;
}

export function deriveShardingAvailability({
  cluster,
  database,
  isDbAlreadyWarm,
  outfmt,
}: DeriveShardingAvailabilityArgs): ShardingAvailability {
  const nodeCount = cluster ? getWorkloadNodeCount(cluster) : null;
  const nodeSku = cluster ? getWorkloadNodeSku(cluster) : null;
  const capacityPlan =
    database?.total_bytes && nodeCount && nodeSku && database.shard_sets?.length
      ? planPartitionsForSubmit(database.total_bytes, nodeCount, nodeSku, database.shard_sets)
      : null;
  const reason = unavailableReason({
    cluster,
    database,
    isDbAlreadyWarm,
    outfmt,
    capacityPlan,
  });
  const enabled = reason == null;
  const hasVerifiedWebSearchSpace = typeof database?.web_blast_searchsp === "number" && database.web_blast_searchsp > 0;
  const preciseReason = reason ?? (hasVerifiedWebSearchSpace ? null : "Verified Web BLAST search-space evidence is not available for this database/options scope.");
  const offDisabled = enabled && isDbAlreadyWarm;
  const offReason = offDisabled
    ? "This database is already warmed as prepared shards on the selected cluster. Use a sharded mode to consume the node-local cache."
    : null;

  return {
    capacityPlan,
    preferredMode: preciseReason == null ? "precise" : enabled ? "approximate" : "off",
    options: {
      off: {
        mode: "off",
        label: "Off",
        enabled: !offDisabled,
        reason: offReason,
        description: "Run against the selected database without partitioned shards. This is the safest baseline and keeps full-DB BLAST semantics, but large databases start slower.",
      },
      approximate: {
        mode: "approximate",
        label: "Fast shard",
        enabled,
        reason,
        description: "Use prepared node-local DB shards and merge top hits. This is a throughput probe mode; full Web BLAST equivalence is not claimed.",
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
