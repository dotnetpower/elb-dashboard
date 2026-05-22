import { describe, expect, it } from "vitest";

import { isAksManagedResourceGroup } from "./aksManagedRg";

describe("isAksManagedResourceGroup", () => {
  it("flags the default MC_ node-RG name", () => {
    expect(
      isAksManagedResourceGroup({
        name: "MC_rg-elb-01_elb-cluster_koreacentral",
      }),
    ).toBe(true);
  });

  it("flags the default ME_ managed-environment RG name", () => {
    expect(
      isAksManagedResourceGroup({
        name: "ME_cae-elb-dashboard-01_abcd_rg-elb-dashboard-01_koreacentral",
      }),
    ).toBe(true);
  });

  it("flags any RG carrying the aks-managed-cluster-name tag", () => {
    expect(
      isAksManagedResourceGroup({
        name: "custom-node-rg",
        tags: { "aks-managed-cluster-name": "elb-cluster" },
      }),
    ).toBe(true);
  });

  it("does not flag ordinary workspace RGs", () => {
    expect(
      isAksManagedResourceGroup({
        name: "rg-elb-01",
        tags: { "elb-storage": "elbstg01", "elb-acr": "elbacr01" },
      }),
    ).toBe(false);
  });

  it("does not flag RGs whose name merely contains MC_ later", () => {
    expect(isAksManagedResourceGroup({ name: "rg-MC_something" })).toBe(false);
  });

  it("does not flag RGs whose name merely contains ME_ later", () => {
    expect(isAksManagedResourceGroup({ name: "rg-ME_something" })).toBe(false);
  });
});
