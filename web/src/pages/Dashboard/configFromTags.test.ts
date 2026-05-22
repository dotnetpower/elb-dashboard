import { describe, expect, it } from "vitest";

import { configFromTags } from "./configFromTags";

describe("configFromTags", () => {
  it("ignores MC_ AKS-managed resource groups that inherited elb tags", () => {
    expect(
      configFromTags("sub", {
        name: "MC_rg-elb-dashboard_aks_koreacentral",
        location: "koreacentral",
        tags: { "elb-storage": "elbstg", "elb-acr": "acrelb" },
      }),
    ).toBeNull();
  });

  it("ignores ME_ managed-environment resource groups that inherited elb tags", () => {
    expect(
      configFromTags("sub", {
        name: "ME_cae-elb-dashboard-01_abcd_rg-elb-dashboard-01_koreacentral",
        location: "koreacentral",
        tags: { "elb-storage": "elbstg", "elb-acr": "acrelb" },
      }),
    ).toBeNull();
  });

  it("returns a workspace config for an ordinary elb-tagged resource group", () => {
    expect(
      configFromTags("sub", {
        name: "rg-elb-dashboard-01",
        location: "koreacentral",
        tags: { "elb-storage": "elbstg", "elb-acr": "acrelb" },
      }),
    ).toMatchObject({
      subscriptionId: "sub",
      workloadResourceGroup: "rg-elb-dashboard-01",
      storageAccountName: "elbstg",
      acrName: "acrelb",
    });
  });
});
