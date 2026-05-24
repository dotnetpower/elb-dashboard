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
    expect(rows[0].shardLabel).toBe("AKS cache shards · 3");
    expect(rows[0].cacheLabel).toBe("AKS cache copying · 0/3");
    expect(rows[0].detail).toContain("Copying");
    expect(rows[0].blockedReason).toBeUndefined();
  });
});