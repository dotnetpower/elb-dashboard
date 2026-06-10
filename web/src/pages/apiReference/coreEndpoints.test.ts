import { describe, expect, it } from "vitest";

import { buildCoreEndpoints } from "./coreEndpoints";

describe("buildCoreEndpoints", () => {
  const ctx = {
    subscriptionId: "sub-1",
    resourceGroup: "rg-1",
    clusterName: "aks-1",
  };

  it("exposes the ensure-running endpoint on the dashboard /api host", () => {
    const endpoints = buildCoreEndpoints(ctx);
    const ensure = endpoints.find(
      (e) => e.path === "/api/aks/openapi/ensure-running",
    );
    expect(ensure).toBeDefined();
    expect(ensure?.method).toBe("post");
    expect(ensure?.tags).toContain("Core");
  });

  it("seeds the request examples with the resolved cluster context", () => {
    const [ensure] = buildCoreEndpoints(ctx);
    const examples = ensure.requestBody?.content?.["application/json"]?.examples;
    expect(examples?.ensure_running?.value).toEqual({
      subscription_id: "sub-1",
      resource_group: "rg-1",
      cluster_name: "aks-1",
    });
    // The observe-only example adds start=false on top of the same context.
    expect(examples?.observe_only?.value).toEqual({
      subscription_id: "sub-1",
      resource_group: "rg-1",
      cluster_name: "aks-1",
      start: false,
    });
  });

  it("documents the polled status vocabulary in the 200 response", () => {
    const [ensure] = buildCoreEndpoints(ctx);
    const ok = ensure.responses?.["200"];
    expect(ok?.fields).toContain("status");
    expect(ok?.fields).toContain("retry_after_seconds");
    expect(ensure.description).toContain("ready");
    expect(ensure.description).toContain("warming");
  });

  it("emphasises that ready waits for warmup, unlike upstream /v1/ready", () => {
    const [ensure] = buildCoreEndpoints(ctx);
    // The endpoint description and the 200 response both call out the
    // stronger-than-/v1/ready semantics so a caller does not assume parity.
    expect(ensure.description).toContain("/v1/ready");
    expect((ensure.description ?? "").toLowerCase()).toContain("warmup");
    expect(ensure.responses?.["200"]?.description).toContain("/v1/ready");
    // The example surfaces a mid-warmup snapshot with progress counters.
    const example = ensure.responses?.["200"]?.example as {
      status?: string;
      warmup?: { ready_node_count?: number; expected_node_count?: number };
    };
    expect(example?.status).toBe("warming");
    expect(example?.warmup?.expected_node_count).toBe(2);
  });

});
