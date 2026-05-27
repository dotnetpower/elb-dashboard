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
    expect(context.candidates).toHaveLength(1);
  });

  it("falls back to the anchor RG before cluster discovery completes", () => {
    const context = resolveApiReferenceClusterContext({
      clusters: [],
      anchorResourceGroup: "rg-elb-dashboard",
    });

    expect(context.clusterName).toBe("");
    expect(context.resourceGroup).toBe("rg-elb-dashboard");
    expect(context.candidates).toEqual([]);
  });

  it("prefers a workload-ready cluster over a stopped one returned earlier by Azure", () => {
    const stopped = cluster({
      name: "elb-cluster-01",
      resource_group: "rg-elb-cluster",
      power_state: "Stopped",
    });
    const running = cluster({
      name: "elb-cluster-small",
      resource_group: "rg-elb-cluster-small",
      power_state: "Running",
    });

    const context = resolveApiReferenceClusterContext({
      clusters: [stopped, running],
      anchorResourceGroup: "rg-elb-dashboard",
    });

    expect(context.clusterName).toBe("elb-cluster-small");
    expect(context.resourceGroup).toBe("rg-elb-cluster-small");
  });

  it("honours the user's preferred cluster name when present in the list", () => {
    const stopped = cluster({
      name: "elb-cluster-01",
      resource_group: "rg-elb-cluster",
      power_state: "Stopped",
    });
    const running = cluster({
      name: "elb-cluster-small",
      resource_group: "rg-elb-cluster-small",
      power_state: "Running",
    });

    const context = resolveApiReferenceClusterContext({
      clusters: [stopped, running],
      anchorResourceGroup: "rg-elb-dashboard",
      preferredClusterName: "elb-cluster-01",
    });

    // User explicitly chose the stopped cluster — respect that so the
    // page can render its "stopped" panel with a Start CTA.
    expect(context.clusterName).toBe("elb-cluster-01");
    expect(context.resourceGroup).toBe("rg-elb-cluster");
  });

  it("ignores a stale preferred name that is no longer in the list", () => {
    const running = cluster({
      name: "elb-cluster-small",
      resource_group: "rg-elb-cluster-small",
      power_state: "Running",
    });

    const context = resolveApiReferenceClusterContext({
      clusters: [running],
      anchorResourceGroup: "rg-elb-dashboard",
      preferredClusterName: "elb-cluster-deleted",
    });

    expect(context.clusterName).toBe("elb-cluster-small");
  });
});