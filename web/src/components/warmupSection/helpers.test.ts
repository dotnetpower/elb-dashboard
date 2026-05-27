import { describe, expect, it } from "vitest";

import { buildWarmupRows, type WarmupCapacity } from "./helpers";

const capacity: WarmupCapacity = {
  nodes: 3,
  memoryPct: 2,
  minFreeGiB: 240,
  memoryPressure: false,
  pressureFlags: [],
};

describe("buildWarmupRows", () => {
  it("does not call a running AKS warmup database not downloaded", () => {
    const rows = buildWarmupRows({
      databases: [],
      warmupDbs: [
        {
          name: "core_nt",
          mol_type: "nucl",
          status: "Loading",
          nodes_ready: 0,
          nodes_failed: 0,
          nodes_active: 3,
          total_jobs: 3,
          shards: ["00", "01", "02"],
          progress_pct: 75.2,
          active_phase: "copying_files",
          active_message: "75.2 %, 240 Done, 0 Failed, 24 Pending",
          elapsed_seconds: 23 * 60,
        },
      ],
      planByName: new Map(),
      capacity,
    });

    expect(rows).toHaveLength(1);
    expect(rows[0].storageLabel).toBe("Storage DB ready");
    expect(rows[0].storageTone).toBe("ok");
    expect(rows[0].shardLabel).toBe("AKS cache shards · 3");
    expect(rows[0].cacheLabel).toBe("AKS cache copying · 0/3");
    expect(rows[0].detail).toContain("Copying");
    expect(rows[0].blockedReason).toBeUndefined();
  });

  it("treats a downloaded DB with copy_status=copying as not ready and blocks Warm", () => {
    const rows = buildWarmupRows({
      databases: [
        {
          name: "core_nt",
          container: "blast-db",
          file_count: 30,
          total_bytes: 12_000_000_000,
          copy_status: { phase: "copying", success: 30, total_files: 800 },
        },
      ],
      warmupDbs: [],
      planByName: new Map(),
      capacity,
    });

    expect(rows).toHaveLength(1);
    const row = rows[0];
    expect(row.storageReady).toBe(false);
    expect(row.storageLabel).toBe("Downloading · 30/800 files");
    expect(row.storageTone).toBe("loading");
    expect(row.canWarm).toBe(false);
    expect(row.primaryAction).toBe("none");
    expect(row.blockedReason).toMatch(/Download in progress/);
    expect(row.detail).toMatch(/Download in progress/);
  });

  it("treats a downloaded DB with copy_status=partial as blocked", () => {
    const rows = buildWarmupRows({
      databases: [
        {
          name: "core_nt",
          container: "blast-db",
          copy_status: { phase: "partial", success: 700, total_files: 800, failed: 50 },
        },
      ],
      warmupDbs: [],
      planByName: new Map(),
      capacity,
    });

    expect(rows[0].storageReady).toBe(false);
    expect(rows[0].storageLabel).toBe("Partial copy · 700/800 files");
    expect(rows[0].storageTone).toBe("blocked");
    expect(rows[0].canWarm).toBe(false);
    expect(rows[0].blockedReason).toMatch(/Retry/);
  });

  it("treats update_in_progress (no copy_status) as not ready, surfaces target", () => {
    const rows = buildWarmupRows({
      databases: [
        {
          name: "core_nt",
          container: "blast-db",
          file_count: 800,
          update_in_progress: true,
          updating_to_source_version: "BLAST_DB-2026-05-20",
        },
      ],
      warmupDbs: [],
      planByName: new Map(),
      capacity,
    });

    expect(rows[0].storageReady).toBe(false);
    expect(rows[0].storageLabel).toBe("Updating to BLAST_DB-2026-05-20");
    expect(rows[0].storageTone).toBe("loading");
    expect(rows[0].canWarm).toBe(false);
  });

  it("keeps legacy file_count>0 DBs (no copy_status) ready", () => {
    const rows = buildWarmupRows({
      databases: [
        {
          name: "16S_ribosomal_RNA",
          container: "blast-db",
          file_count: 12,
          total_bytes: 18_000_000,
        },
      ],
      warmupDbs: [],
      planByName: new Map(),
      capacity,
    });

    expect(rows[0].storageReady).toBe(true);
    expect(rows[0].storageLabel).toBe("Storage DB ready");
    expect(rows[0].storageTone).toBe("ok");
    expect(rows[0].canWarm).toBe(true);
    expect(rows[0].primaryAction).toBe("warm");
  });

  it("treats copy_status=completed as ready and enables Warm", () => {
    const rows = buildWarmupRows({
      databases: [
        {
          name: "core_nt",
          container: "blast-db",
          file_count: 800,
          copy_status: { phase: "completed", success: 800, total_files: 800 },
        },
      ],
      warmupDbs: [],
      planByName: new Map(),
      capacity,
    });

    expect(rows[0].storageReady).toBe(true);
    expect(rows[0].storageLabel).toBe("Storage DB ready");
    expect(rows[0].canWarm).toBe(true);
    expect(rows[0].primaryAction).toBe("warm");
  });
});