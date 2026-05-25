import { describe, expect, it } from "vitest";

import type { AksClusterSummary } from "@/api/endpoints";
import { resolveApiReferenceClusterContext } from "./clusterContext";

function cluster(overrides: Partial<AksClusterSummary>): AksClusterSummary {
  return {
    name: "elb-cluster-01",
    resource_group: "rg-elb-cluster",
    region: "koreacentral",
    k8s_version: "1.34",
    provisioning_state: "Succeeded",
    power_state: "Running",
    node_count: 11,
    node_sku: "Standard_D8s_v5",
    kubelet_object_id: null,
    ...overrides,
  };
}

describe("resolveApiReferenceClusterContext", () => {
  it("uses the selected cluster resource group instead of the dashboard anchor RG", () => {
    const context = resolveApiReferenceClusterContext({
      clusters: [cluster({ resource_group: "rg-elb-cluster" })],
      anchorResourceGroup: "rg-elb-dashboard",
    });

    expect(context.clusterName).toBe("elb-cluster-01");
    expect(context.resourceGroup).toBe("rg-elb-cluster");
  });

  it("falls back to the anchor RG before cluster discovery completes", () => {
    const context = resolveApiReferenceClusterContext({
      clusters: [],
      anchorResourceGroup: "rg-elb-dashboard",
    });

    expect(context.clusterName).toBe("");
    expect(context.resourceGroup).toBe("rg-elb-dashboard");
  });
});