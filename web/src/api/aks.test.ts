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
      "stelbdashboardtest01",
      "rg-elb-dashboard",
      "rg-elbacr",
    );

    expect(mocks.post).toHaveBeenCalledWith("/aks/openapi/deploy", {
      subscription_id: "sub-1",
      resource_group: "rg-elb-cluster",
      cluster_name: "elb-cluster-01",
      acr_name: "elbacr",
      acr_resource_group: "rg-elbacr",
      storage_account: "stelbdashboardtest01",
      storage_resource_group: "rg-elb-dashboard",
    });
  });

  it("posts to the openapi deploy cancel route with the URL-encoded task id", async () => {
    mocks.post.mockResolvedValue({
      task_id: "task with space",
      job_id: null,
      previous_status: "STARTED",
      was_running: true,
      cancelled: true,
      settle_after_seconds: 10,
    });

    const result = await aksApi.cancelOpenApiDeploy("task with space");

    // The route is /aks/openapi/deploy/{task_id}/cancel — the SPA must
    // URL-encode the id so a task id ever ending up with /, ?, # etc.
    // does not corrupt the path. Mirrors aksApi.cancelProvision.
    expect(mocks.post).toHaveBeenCalledWith(
      "/aks/openapi/deploy/task%20with%20space/cancel",
      {},
    );
    expect(result.was_running).toBe(true);
    expect(result.settle_after_seconds).toBe(10);
  });

  it("forwards confirm_recreate=true when the PLS banner button enqueues a recreate", async () => {
    // Issue #22 — the PLS transition banner calls aksApi.deployOpenApi
    // with confirmRecreate=true so the api sidecar's deploy task can
    // delete + recreate the elb-openapi Service to attach the PLS
    // annotation. Without this flag the task aborts at the PLS gate.
    mocks.post.mockResolvedValue({ id: "task-pls-1" });

    await aksApi.deployOpenApi(
      "sub-1",
      "rg-elb-cluster",
      "elb-cluster-01",
      "elbacr",
      "stelbdashboardtest01",
      "rg-elb-dashboard",
      "rg-elbacr",
      true,
    );

    expect(mocks.post).toHaveBeenCalledWith("/aks/openapi/deploy", {
      subscription_id: "sub-1",
      resource_group: "rg-elb-cluster",
      cluster_name: "elb-cluster-01",
      acr_name: "elbacr",
      acr_resource_group: "rg-elbacr",
      storage_account: "stelbdashboardtest01",
      storage_resource_group: "rg-elb-dashboard",
      confirm_recreate: true,
    });
  });

  it("omits confirm_recreate when the flag is false / undefined", async () => {
    // Default path stays a clean POST so the route does not start
    // seeing spurious confirm_recreate fields from every deploy click.
    mocks.post.mockResolvedValue({ id: "task-default" });

    await aksApi.deployOpenApi(
      "sub-1",
      "rg-elb-cluster",
      "elb-cluster-01",
      "elbacr",
      "stelbdashboardtest01",
      "rg-elb-dashboard",
      "rg-elbacr",
      false,
    );

    expect(mocks.post).toHaveBeenCalledWith("/aks/openapi/deploy", {
      subscription_id: "sub-1",
      resource_group: "rg-elb-cluster",
      cluster_name: "elb-cluster-01",
      acr_name: "elbacr",
      acr_resource_group: "rg-elbacr",
      storage_account: "stelbdashboardtest01",
      storage_resource_group: "rg-elb-dashboard",
    });
  });
});
