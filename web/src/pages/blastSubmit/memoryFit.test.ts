import { describe, expect, it } from "vitest";

import type { AksClusterSummary, BlastDatabase } from "@/api/endpoints";

import { deriveFullDbMemoryFit } from "./memoryFit";

// core_nt's BLASTDB bytes-to-cache — the exact 251.7 GB figure ElasticBLAST
// reports. Used to verify we block on E16s_v5 (128 GB) but NOT on E32s_v5
// (256 GB), matching ElasticBLAST's own pre-flight decision.
const CORE_NT_BYTES_TO_CACHE = Math.round(251.7 * 1024 ** 3);

function db(overrides: Partial<BlastDatabase> = {}): BlastDatabase {
  return {
    name: "core_nt",
    container: "blast-db",
    bytes_to_cache: CORE_NT_BYTES_TO_CACHE,
    ...overrides,
  };
}

function cluster(nodeSku: string): AksClusterSummary {
  return { name: "elb-cluster", node_sku: nodeSku } as AksClusterSummary;
}

describe("deriveFullDbMemoryFit", () => {
  it("blocks a full-DB run that exceeds the node's RAM", () => {
    const fit = deriveFullDbMemoryFit({
      database: db(),
      cluster: cluster("Standard_E16s_v5"), // 128 GB
      shardingMode: "off",
    });
    expect(fit.fits).toBe(false);
    expect(fit.blockedReason).toContain("Sharded throughput");
    expect(fit.blockedReason).toContain("core_nt");
  });

  it("does NOT false-block when the same DB fits a larger node", () => {
    const fit = deriveFullDbMemoryFit({
      database: db(),
      cluster: cluster("Standard_E32s_v5"), // 256 GB > 251.7 GB
      shardingMode: "off",
    });
    expect(fit.fits).toBe(true);
    expect(fit.blockedReason).toBeNull();
  });

  it("subtracts the 2 GB system reserve like ElasticBLAST (boundary)", () => {
    // 127 GB on a 128 GB node: usable = 128 - 2 = 126 GB → must block.
    const over = deriveFullDbMemoryFit({
      database: db({ bytes_to_cache: Math.round(127 * 1024 ** 3) }),
      cluster: cluster("Standard_E16s_v5"),
      shardingMode: "off",
    });
    expect(over.fits).toBe(false);
    expect(over.blockedReason).toContain("system reserve");
    // 125 GB fits the 126 GB usable budget → must pass.
    const under = deriveFullDbMemoryFit({
      database: db({ bytes_to_cache: Math.round(125 * 1024 ** 3) }),
      cluster: cluster("Standard_E16s_v5"),
      shardingMode: "off",
    });
    expect(under.fits).toBe(true);
    expect(under.blockedReason).toBeNull();
  });

  it("never blocks for a sharded execution profile", () => {
    const fit = deriveFullDbMemoryFit({
      database: db(),
      cluster: cluster("Standard_E16s_v5"),
      shardingMode: "precise",
    });
    expect(fit.fits).toBeNull();
    expect(fit.blockedReason).toBeNull();
  });

  it("does not block when bytes_to_cache is unknown", () => {
    const fit = deriveFullDbMemoryFit({
      database: db({ bytes_to_cache: undefined }),
      cluster: cluster("Standard_E16s_v5"),
      shardingMode: "off",
    });
    expect(fit.fits).toBeNull();
    expect(fit.blockedReason).toBeNull();
  });

  it("does not block when the node SKU is unrecognised", () => {
    const fit = deriveFullDbMemoryFit({
      database: db(),
      cluster: cluster("Standard_Mystery_Sku"),
      shardingMode: "off",
    });
    expect(fit.fits).toBeNull();
    expect(fit.blockedReason).toBeNull();
  });

  it("does not block when database or cluster is missing", () => {
    expect(
      deriveFullDbMemoryFit({ database: undefined, cluster: cluster("Standard_E16s_v5"), shardingMode: "off" }).fits,
    ).toBeNull();
    expect(
      deriveFullDbMemoryFit({ database: db(), cluster: undefined, shardingMode: "off" }).fits,
    ).toBeNull();
  });
});
