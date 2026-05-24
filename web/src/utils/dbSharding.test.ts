import { describe, expect, it } from "vitest";

import { normalizeSkuName, planPartitionsForSubmit } from "@/utils/dbSharding";

describe("db sharding SKU normalization", () => {
  it("accepts Azure SKU aliases in capacity previews", () => {
    expect(normalizeSkuName("E32as_v7")).toBe("Standard_E32as_v7");
    expect(normalizeSkuName(" standard_e32as_v7 ")).toBe("Standard_E32as_v7");

    const plan = planPartitionsForSubmit(
      175.5 * 1024 ** 3,
      3,
      "E32as_v7",
      [1, 2, 3, 4, 5, 6, 8, 10],
    );

    expect(plan.feasible).toBe(true);
    expect(plan.machineType).toBe("Standard_E32as_v7");
    expect(plan.nodeRamGib).toBe(256);
    expect(plan.safeRamPerShardGib).toBe(128);
    expect(plan.pickedN).toBe(3);
  });

  it("keeps unknown SKUs on the conservative fallback", () => {
    const plan = planPartitionsForSubmit(269 * 1024 ** 3, 1, "NotARealSku");

    expect(plan.machineType).toBe("NotARealSku");
    expect(plan.nodeRamGib).toBe(64);
    expect(plan.pickedN).toBe(10);
  });
});