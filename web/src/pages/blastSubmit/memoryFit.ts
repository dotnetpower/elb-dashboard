// Pure derivation of whether a full-database (non-sharded) BLAST fits node RAM.
//
// Responsibility: Mirror ElasticBLAST's submit pre-flight memory check on the
//   client so the submit form can block a full-DB run that would be rejected at
//   runtime ("memory requirements exceed memory available on selected machine
//   type ..."). Only the `off` execution profile (Baseline / Warmed database)
//   loads the full DB into one node; the sharded profile partitions it.
// Edit boundaries: Pure functions only — no React, no I/O. The required-memory
//   number is the DB's BLASTDB `bytes_to_cache` (the exact value ElasticBLAST
//   compares), so the verdict neither false-blocks a DB ElasticBLAST would
//   accept (e.g. core_nt 251.7 GB on Standard_E32s_v5 / 256 GB) nor passes one
//   it would reject. Keep the threshold a direct `required <= nodeRam` compare
//   in lockstep with `api/services/blast/submit_gates.py::_gate_node_memory_fit`.
// Key entry points: `deriveFullDbMemoryFit`, `fullDbMemoryWarmupRemediation`,
//   `fullDbMemoryWarmingInProgress`.
// Risky contracts: `fits === null` means "unknown" (no authoritative number) and
//   MUST NOT block — the backend gate and ElasticBLAST's own pre-flight remain
//   the net. `blockedReason` is user-visible; tests assert its shape.
// Validation: `npx vitest run src/pages/blastSubmit/memoryFit.test.ts`.

import type { AksClusterSummary, BlastDatabase } from "@/api/endpoints";
import { getWorkloadNodeSku } from "@/pages/blastSubmit/computeEnvironment";
import { bytesToGib, normalizeSkuName, SKU_MEMORY_GIB } from "@/utils/dbSharding";

import type { ShardingMode } from "@/pages/blastSubmit/shardingAvailability";

/**
 * GiB ElasticBLAST reserves for the OS before fitting a database into a node.
 * Mirrors `SYSTEM_MEMORY_RESERVE` in the sibling `elastic-blast-azure` repo
 * (constants.py — interim value 2) and the backend `_SYSTEM_MEMORY_RESERVE_GIB`
 * in `api/services/blast/submit_gates.py`. ElasticBLAST's full-DB pre-flight
 * rejects when `nodeRam - reserve < requiredGib`, so subtract the same reserve
 * here to match its verdict exactly (no false-block, no false-pass).
 */
export const SYSTEM_MEMORY_RESERVE_GIB = 2;

export interface FullDbMemoryFit {
  /**
   * `true` fits, `false` does not fit (block), `null` unknown — do not block.
   * Unknown covers: sharded profile (check N/A), missing `bytes_to_cache`, or an
   * unrecognised node SKU whose RAM we cannot look up.
   */
  fits: boolean | null;
  requiredGib: number | null;
  nodeRamGib: number | null;
  /** Non-null only when `fits === false`. Actionable; steers to Sharded throughput. */
  blockedReason: string | null;
}

const UNKNOWN: FullDbMemoryFit = {
  fits: null,
  requiredGib: null,
  nodeRamGib: null,
  blockedReason: null,
};

/**
 * Decide whether the selected database fits a single workload node's RAM for a
 * full-database BLAST. Returns `fits: null` (no block) whenever the inputs are
 * insufficient to make an authoritative call.
 */
export function deriveFullDbMemoryFit(args: {
  database?: BlastDatabase;
  cluster?: AksClusterSummary;
  shardingMode: ShardingMode;
}): FullDbMemoryFit {
  const { database, cluster, shardingMode } = args;

  // Only the full-DB (off) profile loads the whole database into one node.
  if (shardingMode !== "off") return UNKNOWN;
  if (!database || !cluster) return UNKNOWN;

  const bytesToCache = database.bytes_to_cache;
  if (typeof bytesToCache !== "number" || bytesToCache <= 0) return UNKNOWN;

  const skuName = getWorkloadNodeSku(cluster);
  const nodeRamGib = skuName ? SKU_MEMORY_GIB[normalizeSkuName(skuName)] : undefined;
  if (typeof nodeRamGib !== "number" || nodeRamGib <= 0) return UNKNOWN;

  const requiredGib = bytesToGib(bytesToCache);
  const usableGib = nodeRamGib - SYSTEM_MEMORY_RESERVE_GIB;
  const fits = requiredGib <= usableGib;

  return {
    fits,
    requiredGib,
    nodeRamGib,
    blockedReason: fits
      ? null
      : `'${database.name}' needs ${requiredGib.toFixed(1)} GB for a full-database ` +
        `BLAST, which loads the entire database into a single node — adding more ` +
        `nodes does not help. The cluster node (${skuName}) provides only ` +
        `${usableGib.toFixed(0)} GB usable (${nodeRamGib.toFixed(0)} GB RAM minus ` +
        `${SYSTEM_MEMORY_RESERVE_GIB} GB system reserve). Switch to the Sharded ` +
        `throughput execution profile to spread the database across your nodes, or ` +
        `use a cluster with a larger machine type.`,
  };
}

/**
 * Alternate remediation for the full-DB memory block when the Sharded
 * throughput profile is currently disabled *only* because the database is not
 * warm yet (see `ShardingAvailability.canUnlockShardingByWarming`). The default
 * `blockedReason` tells the user to "Switch to the Sharded throughput execution
 * profile", but in this state that control is greyed out — a catch-22. This
 * message instead steers them to the actionable step (warm the database, which
 * enables sharding) while keeping the larger-machine alternative.
 *
 * Returns `null` unless `fit.fits === false` with known numbers, so callers can
 * fall through to the default `blockedReason`.
 */
export function fullDbMemoryWarmupRemediation(
  fit: FullDbMemoryFit,
  dbName: string,
): string | null {
  if (fit.fits !== false || fit.requiredGib == null || fit.nodeRamGib == null) {
    return null;
  }
  const usableGib = fit.nodeRamGib - SYSTEM_MEMORY_RESERVE_GIB;
  return (
    `'${dbName}' needs ${fit.requiredGib.toFixed(1)} GB and does not fit a single ` +
    `node's ${usableGib.toFixed(0)} GB usable for a full-database BLAST. Warm this ` +
    `database on the selected cluster to enable the Sharded throughput profile, ` +
    `which spreads it across your nodes — or use a cluster with a larger machine type.`
  );
}

/**
 * Remediation for the full-DB memory block when an explicit warmup is ALREADY
 * running on the selected cluster for this database. Both the default
 * `blockedReason` and `fullDbMemoryWarmupRemediation` tell the user to "warm
 * this database" — but when a warmup Job is mid-flight that reads as "do the
 * thing you are already doing". This message instead reassures the user that
 * the warmup in progress will unlock the Sharded throughput profile (which
 * spreads the database across nodes) and that they should submit once it
 * completes. Waiting — not Baseline — is the path, because a full-database
 * (Baseline) run loads the entire DB into one node and can never fit here.
 *
 * `progressPct` (0–100) is folded into the message only when it is a partial
 * value, so a missing or boundary number degrades to no progress hint instead
 * of a misleading "0% / 100%".
 *
 * Returns `null` unless `fit.fits === false` with known numbers, so callers can
 * fall through to the other remediations.
 */
export function fullDbMemoryWarmingInProgress(
  fit: FullDbMemoryFit,
  dbName: string,
  progressPct?: number | null,
): string | null {
  if (fit.fits !== false || fit.requiredGib == null || fit.nodeRamGib == null) {
    return null;
  }
  const progress =
    typeof progressPct === "number" && progressPct > 0 && progressPct < 100
      ? ` (${progressPct.toFixed(0)}% complete)`
      : "";
  return (
    `'${dbName}' is warming on the selected cluster now${progress}. When it ` +
    `finishes, the Sharded throughput profile unlocks and spreads its ` +
    `${fit.requiredGib.toFixed(1)} GB across your nodes — wait for the warmup to ` +
    `complete, then submit. A full-database (Baseline) run loads the entire ` +
    `database into a single node, so it cannot run '${dbName}' here.`
  );
}
