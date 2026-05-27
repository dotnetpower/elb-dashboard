import { describe, expect, it } from "vitest";

import type { AksClusterSummary } from "@/api/monitoring";
import { pickPreferredCluster } from "@/utils/clusterSelection";

function cluster(
  partial: Partial<AksClusterSummary> & { name: string },
): AksClusterSummary {
  return {
    resource_group: partial.resource_group ?? "rg-default",
    region: partial.region ?? "koreacentral",
    power_state: partial.power_state ?? "Stopped",
    provisioning_state: partial.provisioning_state ?? "Succeeded",
    node_count: partial.node_count ?? 0,
    node_sku: partial.node_sku ?? null,
    tier: partial.tier ?? null,
    agent_pools: partial.agent_pools ?? [],
    ...partial,
  } as AksClusterSummary;
}

describe("pickPreferredCluster", () => {
  it("returns undefined when no clusters", () => {
    expect(pickPreferredCluster([])).toBeUndefined();
  });

  it("prefers an exact name match over everything else", () => {
    const stopped = cluster({ name: "elb-heavy", power_state: "Stopped" });
    const running = cluster({ name: "elb-light", power_state: "Running" });
    expect(
      pickPreferredCluster([stopped, running], { name: "elb-heavy" })?.name,
    ).toBe("elb-heavy");
  });

  it("prefers a workload-ready (Running + Succeeded) cluster over the first one", () => {
    const first = cluster({ name: "elb-heavy", power_state: "Stopped" });
    const running = cluster({
      name: "elb-light",
      power_state: "Running",
      provisioning_state: "Succeeded",
    });
    expect(pickPreferredCluster([first, running])?.name).toBe("elb-light");
  });

  it("does not treat Running + Updating as workload-ready", () => {
    const updating = cluster({
      name: "elb-updating",
      power_state: "Running",
      provisioning_state: "Updating",
    });
    const ready = cluster({
      name: "elb-ready",
      power_state: "Running",
      provisioning_state: "Succeeded",
    });
    expect(pickPreferredCluster([updating, ready])?.name).toBe("elb-ready");
  });

  it("falls back to RG match when no Running cluster exists", () => {
    const stoppedOther = cluster({
      name: "elb-other",
      resource_group: "rg-other",
      power_state: "Stopped",
    });
    const stoppedAnchor = cluster({
      name: "elb-anchor",
      resource_group: "rg-anchor",
      power_state: "Stopped",
    });
    expect(
      pickPreferredCluster([stoppedOther, stoppedAnchor], {
        resourceGroup: "rg-anchor",
      })?.name,
    ).toBe("elb-anchor");
  });

  it("falls back to a cluster with nodes when requireNodes is set", () => {
    const empty = cluster({
      name: "elb-empty",
      power_state: "Stopped",
      node_count: 0,
    });
    const withNodes = cluster({
      name: "elb-loaded",
      power_state: "Stopped",
      node_count: 3,
    });
    expect(
      pickPreferredCluster([empty, withNodes], { requireNodes: true })?.name,
    ).toBe("elb-loaded");
  });

  it("falls back to the first cluster as last resort", () => {
    const a = cluster({ name: "a", power_state: "Stopped" });
    const b = cluster({ name: "b", power_state: "Stopped" });
    expect(pickPreferredCluster([a, b])?.name).toBe("a");
  });

  it("name match wins even if the named cluster is Stopped and another is Running", () => {
    // Intentional caller pin (e.g. SettingsPanel user picked it) — the
    // helper must not override the explicit choice.
    const named = cluster({ name: "elb-pinned", power_state: "Stopped" });
    const running = cluster({ name: "elb-other", power_state: "Running" });
    expect(
      pickPreferredCluster([named, running], { name: "elb-pinned" })?.name,
    ).toBe("elb-pinned");
  });
});
