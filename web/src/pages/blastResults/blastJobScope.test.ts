import { describe, expect, it } from "vitest";

import { resolveBlastJobScope } from "./blastJobScope";

const EMPTY_PARAMS = new URLSearchParams();

const ANCHOR = {
  subscriptionId: "anchor-sub",
  storageAccountName: "anchorstorage",
  workloadResourceGroup: "rg-elb-dashboard",
};

describe("resolveBlastJobScope", () => {
  it("uses the job's infrastructure RG/cluster when the payload omits them (cross-RG cluster)", () => {
    const scope = resolveBlastJobScope({
      searchParams: EMPTY_PARAMS,
      payload: {},
      infrastructure: {
        subscription_id: "anchor-sub",
        storage_account: "anchorstorage",
        resource_group: "rg-elb-cluster",
        cluster_name: "elb-cluster-02",
      },
      config: ANCHOR,
    });

    expect(scope.resourceGroup).toBe("rg-elb-cluster");
    expect(scope.clusterName).toBe("elb-cluster-02");
  });

  it("does NOT fall back to the workspace anchor RG when infrastructure has the cluster RG", () => {
    const scope = resolveBlastJobScope({
      searchParams: EMPTY_PARAMS,
      payload: undefined,
      infrastructure: { resource_group: "rg-elb-cluster" },
      config: ANCHOR,
    });

    expect(scope.resourceGroup).toBe("rg-elb-cluster");
    expect(scope.resourceGroup).not.toBe(ANCHOR.workloadResourceGroup);
  });

  it("prefers the submit payload over infrastructure and anchor", () => {
    const scope = resolveBlastJobScope({
      searchParams: EMPTY_PARAMS,
      payload: {
        subscription_id: "payload-sub",
        storage_account: "payloadstorage",
        resource_group: "rg-payload",
        cluster_name: "payload-cluster",
      },
      infrastructure: { resource_group: "rg-infra", cluster_name: "infra-cluster" },
      config: ANCHOR,
    });

    expect(scope).toEqual({
      subscriptionId: "payload-sub",
      storageAccount: "payloadstorage",
      resourceGroup: "rg-payload",
      clusterName: "payload-cluster",
    });
  });

  it("prefers explicit URL query params over everything else", () => {
    const scope = resolveBlastJobScope({
      searchParams: new URLSearchParams({
        subscription_id: "url-sub",
        storage_account: "urlstorage",
        resource_group: "rg-url",
        cluster_name: "url-cluster",
      }),
      payload: { resource_group: "rg-payload" },
      infrastructure: { resource_group: "rg-infra" },
      config: ANCHOR,
    });

    expect(scope).toEqual({
      subscriptionId: "url-sub",
      storageAccount: "urlstorage",
      resourceGroup: "rg-url",
      clusterName: "url-cluster",
    });
  });

  it("accepts the legacy aks_cluster_name payload alias", () => {
    const scope = resolveBlastJobScope({
      searchParams: EMPTY_PARAMS,
      payload: { aks_cluster_name: "legacy-cluster" },
      infrastructure: undefined,
      config: ANCHOR,
    });

    expect(scope.clusterName).toBe("legacy-cluster");
  });

  it("falls back to the workspace anchor config only when payload and infrastructure are empty", () => {
    const scope = resolveBlastJobScope({
      searchParams: EMPTY_PARAMS,
      payload: undefined,
      infrastructure: undefined,
      config: ANCHOR,
    });

    expect(scope.subscriptionId).toBe("anchor-sub");
    expect(scope.storageAccount).toBe("anchorstorage");
    expect(scope.resourceGroup).toBe("rg-elb-dashboard");
    // No cluster signal anywhere: stays empty on purpose. A wrong guess made
    // cancel target a non-existent cluster (`cancel_unavailable`); external
    // jobs are cancelled via the sibling, which owns its own cluster.
    expect(scope.clusterName).toBe("");
  });
});
