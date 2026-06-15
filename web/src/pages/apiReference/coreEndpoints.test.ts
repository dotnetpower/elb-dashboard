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

  it("exposes the cluster-independent database list endpoint on the dashboard host", () => {
    const list = buildCoreEndpoints(ctx).find(
      (e) => e.path === "/api/aks/openapi/databases",
    );
    expect(list).toBeDefined();
    expect(list?.method).toBe("get");
    expect(list?.tags).toContain("Core");
    // elb-openapi drop-in list shape.
    expect(list?.responses?.["200"]?.fields).toEqual([
      "databases",
      "count",
      "container",
    ]);
    // No input params: the deployed api sidecar resolves the Storage scope
    // from its env, so the list call is one-click with nothing to fill in.
    expect(list?.parameters).toEqual([]);
  });

  it("exposes the cluster-independent database metadata endpoint with a seeded path default", () => {
    const detail = buildCoreEndpoints(ctx).find(
      (e) => e.path === "/api/aks/openapi/databases/{db_name}",
    );
    expect(detail).toBeDefined();
    expect(detail?.method).toBe("get");
    expect(detail?.tags).toContain("Core");
    // The path param carries a default so a one-click "Send Request" builds a
    // valid URL instead of a broken `/databases/`.
    const dbName = detail?.parameters.find((p) => p.name === "db_name");
    expect(dbName?.in).toBe("path");
    expect(dbName?.required).toBe(true);
    expect(dbName?.schema?.default).toBe("core_nt");
    // Only db_name is needed — the Storage scope comes from the api sidecar
    // env, so no query params are exposed.
    expect(detail?.parameters.every((p) => p.in === "path")).toBe(true);
    // Drop-in for elb-openapi DatabaseMetadata.
    expect(detail?.responses?.["200"]?.fields).toContain("molecule_type");
    expect(detail?.responses?.["404"]).toBeDefined();
  });

  it("keeps ensure-running first so the other tests' [0] index holds", () => {
    expect(buildCoreEndpoints(ctx)[0].path).toBe(
      "/api/aks/openapi/ensure-running",
    );
  });
});
