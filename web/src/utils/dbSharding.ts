/**
 * Client-side mirror of `api/services/db_sharding.py::select_partitions_for_submit`.
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
const SKU_MEMORY_GIB: Record<string, number> = {
  // E v5 (memory-optimised, our default workload pool)
  Standard_E16s_v5: 128,
  Standard_E32s_v5: 256,
  Standard_E48s_v5: 384,
  Standard_E64s_v5: 512,
  // E bs v5 (NVMe local)
  Standard_E16bs_v5: 128,
  Standard_E32bs_v5: 256,
  Standard_E48bs_v5: 384,
  Standard_E64bs_v5: 512,
  // L v3 (storage-optimised)
  Standard_L16s_v3: 128,
  Standard_L32s_v3: 256,
  Standard_L48s_v3: 384,
  Standard_L64s_v3: 512,
  // System pool fallbacks
  Standard_D2s_v3: 8,
  Standard_D4s_v3: 16,
};

/** Convert bytes to GiB (binary, matches Python `bytes / 1024**3`). */
function bytesToGib(bytes: number): number {
  return bytes / 1024 ** 3;
}

/**
 * Pick the smallest preset N satisfying both:
 *   1. N >= numNodes  (one shard per node, no idle nodes)
 *   2. (db_total_gib / N) <= node_ram_gib * SAFE_SHARD_FRACTION_OF_NODE_RAM
 *      (per-shard working set fits in safe RAM headroom)
 *
 * If no preset satisfies both, returns the largest preset (best effort).
 *
 * Mirrors `select_partitions_for_submit` in `api/services/db_sharding.py`.
 */
export function selectPartitionsForSubmit(
  dbTotalBytes: number,
  numNodes: number,
  machineType: string,
  presets: readonly number[] = PRESET_SHARD_SETS,
): number {
  const ramGib = SKU_MEMORY_GIB[machineType] ?? 64;
  const safeRamPerShard = ramGib * SAFE_SHARD_FRACTION_OF_NODE_RAM;
  const dbGib = bytesToGib(dbTotalBytes);
  const minByRam = safeRamPerShard > 0 ? Math.ceil(dbGib / safeRamPerShard) : numNodes;
  const minN = Math.max(numNodes, minByRam, 1);
  for (const n of presets) {
    if (n >= minN) return n;
  }
  return presets[presets.length - 1] ?? 1;
}
