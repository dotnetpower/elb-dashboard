/**
 * Client-side mirror of `api/services/db/sharding.py::select_partitions_for_submit`.
 *
 * Used by the submit form to render an "auto-shard preview" so the user can
 * see which preset N will be applied by the backend before they click Submit.
 *
 * Source of truth is the Python helper — keep these constants in sync.
 */

/** Preset shard counts pre-built by `prepare-db`. Must match Python. */
export const PRESET_SHARD_SETS: readonly number[] = [1, 2, 3, 4, 5, 6, 8, 10];

/** Fraction of node RAM that a single shard's working set is allowed to occupy. */
export const SAFE_SHARD_FRACTION_OF_NODE_RAM = 0.5;

/**
 * Memory (GiB) for the AKS SKUs we actively support. Values mirror
 * `api/services/aks_skus.py::SKU_CATALOG`. Unknown SKUs fall back to
 * the same 64 GiB default the backend uses, so the preview never throws.
 */
export const SKU_MEMORY_GIB: Record<string, number> = {
  // HPC
  Standard_HB120rs_v3: 480,
  Standard_HC44rs: 352,
  Standard_HB60rs: 240,
  // D v3
  Standard_D8s_v3: 32,
  Standard_D16s_v3: 64,
  Standard_D32s_v3: 128,
  Standard_D64s_v3: 256,
  // E v3
  Standard_E64s_v3: 432,
  Standard_E64is_v3: 504,
  // E v5 (memory-optimised, our default workload pool)
  Standard_E16s_v5: 128,
  Standard_E32s_v5: 256,
  Standard_E48s_v5: 384,
  Standard_E64s_v5: 512,
  Standard_E96s_v5: 672,
  // E as v7 (available in subscriptions where E v5 is restricted)
  Standard_E16as_v7: 128,
  Standard_E32as_v7: 256,
  Standard_E48as_v7: 384,
  // E bs v5 (NVMe local)
  Standard_E16bs_v5: 128,
  Standard_E32bs_v5: 256,
  Standard_E48bs_v5: 384,
  Standard_E64bs_v5: 512,
  Standard_E96bs_v5: 672,
  // L v3 (storage-optimised)
  Standard_L8s_v3: 64,
  Standard_L16s_v3: 128,
  Standard_L32s_v3: 256,
  Standard_L48s_v3: 384,
  Standard_L64s_v3: 512,
  Standard_L80s_v3: 640,
  Standard_L8as_v3: 64,
  Standard_L16as_v3: 128,
  Standard_L32as_v3: 256,
  Standard_L48as_v3: 384,
  Standard_L64as_v3: 512,
  Standard_L80as_v3: 640,
  // System pool fallbacks
  Standard_D2s_v3: 8,
  Standard_D4s_v3: 16,
  Standard_D2as_v7: 8,
  Standard_D4as_v7: 16,
};

const SKU_NAME_BY_CASEFOLD = new Map(
  Object.keys(SKU_MEMORY_GIB).map((skuName) => [skuName.toLowerCase(), skuName]),
);

export function normalizeSkuName(skuName: string | null | undefined): string {
  const raw = (skuName ?? "").trim();
  if (!raw) return "";
  if (SKU_MEMORY_GIB[raw] != null) return raw;

  const canonical = SKU_NAME_BY_CASEFOLD.get(raw.toLowerCase());
  if (canonical) return canonical;

  if (!raw.toLowerCase().startsWith("standard_")) {
    const withPrefix = `Standard_${raw}`;
    return SKU_NAME_BY_CASEFOLD.get(withPrefix.toLowerCase()) ?? raw;
  }

  return raw;
}

/** Convert bytes to GiB (binary, matches Python `bytes / 1024**3`). */
export function bytesToGib(bytes: number): number {
  return bytes / 1024 ** 3;
}

export interface ShardCapacityPlan {
  feasible: boolean;
  pickedN: number;
  minShards: number;
  maxPreset: number;
  numNodes: number;
  machineType: string;
  dbGib: number;
  nodeRamGib: number;
  safeRamPerShardGib: number;
  perShardGib: number;
  reason: string | null;
}

export function planPartitionsForSubmit(
  dbTotalBytes: number,
  numNodes: number,
  machineType: string,
  presets: readonly number[] = PRESET_SHARD_SETS,
): ShardCapacityPlan {
  const safeNumNodes = Math.max(1, Math.trunc(numNodes || 1));
  const normalizedMachineType = normalizeSkuName(machineType);
  const nodeRamGib = SKU_MEMORY_GIB[normalizedMachineType] ?? 64;
  const safeRamPerShardGib = nodeRamGib * SAFE_SHARD_FRACTION_OF_NODE_RAM;
  const dbGib = bytesToGib(Math.max(0, dbTotalBytes));
  const minByRam = safeRamPerShardGib > 0 ? Math.ceil(dbGib / safeRamPerShardGib) : safeNumNodes;
  const minShards = Math.max(safeNumNodes, minByRam, 1);
  const sortedPresets = [...presets].filter((n) => n > 0).sort((a, b) => a - b);
  const maxPreset = sortedPresets[sortedPresets.length - 1] ?? 1;
  const pickedN = sortedPresets.find((n) => n >= minShards) ?? maxPreset;
  const feasible = pickedN >= minShards && pickedN <= safeNumNodes;
  const perShardGib = pickedN > 0 ? dbGib / pickedN : dbGib;
  const preparedShardLabel = maxPreset === 1 ? "prepared shard is" : "prepared shards are";
  const reason = feasible
    ? null
    : pickedN > safeNumNodes
      ? `This database needs ${pickedN} shards, but the selected cluster has ${safeNumNodes} workload nodes.`
      : `This database needs at least ${minShards} shards for ${normalizedMachineType}, but only ${maxPreset} ${preparedShardLabel} available.`;

  return {
    feasible,
    pickedN,
    minShards,
    maxPreset,
    numNodes: safeNumNodes,
    machineType: normalizedMachineType,
    dbGib,
    nodeRamGib,
    safeRamPerShardGib,
    perShardGib,
    reason,
  };
}

/**
 * Pick the smallest preset N satisfying both:
 *   1. N >= numNodes  (one shard per node, no idle nodes)
 *   2. (db_total_gib / N) <= node_ram_gib * SAFE_SHARD_FRACTION_OF_NODE_RAM
 *      (per-shard working set fits in safe RAM headroom)
 *
 * If no preset satisfies both, returns the largest preset (best effort).
 *
 * Mirrors `select_partitions_for_submit` in `api/services/db/sharding.py`.
 */
export function selectPartitionsForSubmit(
  dbTotalBytes: number,
  numNodes: number,
  machineType: string,
  presets: readonly number[] = PRESET_SHARD_SETS,
): number {
  return planPartitionsForSubmit(dbTotalBytes, numNodes, machineType, presets).pickedN;
}
