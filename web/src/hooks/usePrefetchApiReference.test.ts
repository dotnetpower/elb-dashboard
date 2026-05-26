import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  aks: vi.fn(),
  acr: vi.fn(),
  serviceIp: vi.fn(),
  proxyOpenApiSpec: vi.fn(),
}));

vi.mock("@/api/endpoints", () => ({
  monitoringApi: {
    aks: mocks.aks,
    acr: mocks.acr,
    serviceIp: mocks.serviceIp,
  },
  aksApi: {
    proxyOpenApiSpec: mocks.proxyOpenApiSpec,
  },
}));

import { prefetchApiReferenceQueries } from "@/hooks/usePrefetchApiReference";

describe("prefetchApiReferenceQueries", () => {
  beforeEach(() => {
    mocks.aks.mockReset();
    mocks.acr.mockReset();
    mocks.serviceIp.mockReset();
    mocks.proxyOpenApiSpec.mockReset();
  });

  it("discovers AKS subscription-wide and uses the cluster RG for OpenAPI prefetches", async () => {
    const cached = new Map<string, unknown>();
    const prefetchedKeys: unknown[][] = [];
    const keyOf = (key: unknown[]) => JSON.stringify(key);
    const qc = {
      async prefetchQuery(options: { queryKey: unknown[]; queryFn: () => unknown }) {
        prefetchedKeys.push(options.queryKey);
        const data = await options.queryFn();
        cached.set(keyOf(options.queryKey), data);
        return data;
      },
      getQueryData<T>(queryKey: unknown[]) {
        return cached.get(keyOf(queryKey)) as T | undefined;
      },
    };

    mocks.aks.mockResolvedValue({
      clusters: [
        {
          name: "elb-cluster-01",
          resource_group: "rg-elb-cluster",
          region: "koreacentral",
          k8s_version: "1.34",
          provisioning_state: "Succeeded",
          power_state: "Running",
          node_count: 11,
          node_sku: "Standard_D8s_v5",
          kubelet_object_id: null,
        },
      ],
    });
    mocks.acr.mockResolvedValue({ actual_tags: { "elb-openapi": ["2026.05.21"] } });
    mocks.serviceIp.mockResolvedValue({ external_ip: "10.42.0.52" });
    mocks.proxyOpenApiSpec.mockResolvedValue({ openapi: "3.1.0" });

    await prefetchApiReferenceQueries(qc, {
      subscriptionId: "sub-1",
      workloadResourceGroup: "rg-elb-dashboard",
      acrResourceGroup: "rg-elbacr",
      acrName: "elbacr",
    });

    expect(mocks.aks).toHaveBeenCalledWith("sub-1");
    expect(mocks.serviceIp).toHaveBeenCalledWith(
      "sub-1",
      "rg-elb-cluster",
      "elb-cluster-01",
      "elb-openapi",
    );
    expect(mocks.proxyOpenApiSpec).toHaveBeenCalledWith(
      "sub-1",
      "rg-elb-cluster",
      "elb-cluster-01",
    );
    expect(prefetchedKeys).toContainEqual(["aks", "sub-1", "sub"]);
    expect(prefetchedKeys).toContainEqual([
      "openapi-svc",
      "sub-1",
      "rg-elb-cluster",
      "elb-cluster-01",
    ]);
    expect(prefetchedKeys).toContainEqual([
      "openapi-spec",
      "sub-1",
      "rg-elb-cluster",
      "elb-cluster-01",
    ]);
  });

  it("does not prefetch the spec when service discovery returns no IP", async () => {
    const cached = new Map<string, unknown>();
    const keyOf = (key: unknown[]) => JSON.stringify(key);
    const qc = {
      async prefetchQuery(options: { queryKey: unknown[]; queryFn: () => unknown }) {
        const data = await options.queryFn();
        cached.set(keyOf(options.queryKey), data);
        return data;
      },
      getQueryData<T>(queryKey: unknown[]) {
        return cached.get(keyOf(queryKey)) as T | undefined;
      },
    };

    mocks.aks.mockResolvedValue({
      clusters: [
        {
          name: "elb-cluster-01",
          resource_group: "rg-elb-cluster",
          region: "koreacentral",
          k8s_version: "1.34",
          provisioning_state: "Succeeded",
          power_state: "Running",
          node_count: 11,
          node_sku: "Standard_D8s_v5",
          kubelet_object_id: null,
        },
      ],
    });
    mocks.acr.mockResolvedValue({ actual_tags: { "elb-openapi": ["2026.05.21"] } });
    mocks.serviceIp.mockResolvedValue({
      service_name: "elb-openapi",
      external_ip: null,
      available: false,
      status: "missing_or_pending",
    });

    await prefetchApiReferenceQueries(qc, {
      subscriptionId: "sub-1",
      workloadResourceGroup: "rg-elb-dashboard",
      acrResourceGroup: "rg-elbacr",
      acrName: "elbacr",
    });

    expect(mocks.serviceIp).toHaveBeenCalled();
    expect(mocks.proxyOpenApiSpec).not.toHaveBeenCalled();
  });
});
