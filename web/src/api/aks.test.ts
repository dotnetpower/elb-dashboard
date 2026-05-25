import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  post: vi.fn(),
}));

vi.mock("@/api/client", () => ({
  api: {
    post: mocks.post,
  },
}));

import { aksApi } from "@/api/aks";

describe("aksApi", () => {
  beforeEach(() => {
    mocks.post.mockReset();
  });

  it("includes storage_resource_group when deploying elb-openapi", async () => {
    mocks.post.mockResolvedValue({ id: "task-1" });

    await aksApi.deployOpenApi(
      "sub-1",
      "rg-elb-cluster",
      "elb-cluster-01",
      "elbacr",
      "stelbdashboardmul5oh5j44",
      "rg-elb-dashboard",
    );

    expect(mocks.post).toHaveBeenCalledWith("/aks/openapi/deploy", {
      subscription_id: "sub-1",
      resource_group: "rg-elb-cluster",
      cluster_name: "elb-cluster-01",
      acr_name: "elbacr",
      storage_account: "stelbdashboardmul5oh5j44",
      storage_resource_group: "rg-elb-dashboard",
    });
  });
});