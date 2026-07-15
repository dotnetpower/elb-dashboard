import { beforeEach, describe, expect, it, vi } from "vitest";

const apiMocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  del: vi.fn(),
}));

vi.mock("@/api/client", () => ({ api: apiMocks }));

import { blastTemplatesApi } from "@/api/blastTemplates";
import { clusterCostApi } from "@/api/cost";
import { notificationsApi } from "@/api/notifications";
import { webhooksApi } from "@/api/webhooks";

describe("typed client path contracts", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("keeps notifications relative to the shared /api prefix", () => {
    notificationsApi.list(25);
    notificationsApi.markSeen();

    expect(apiMocks.get).toHaveBeenCalledWith("/notifications?limit=25");
    expect(apiMocks.post).toHaveBeenCalledWith("/notifications/seen", {});
  });

  it("keeps cost endpoints relative to the shared /api prefix", () => {
    clusterCostApi.get("sub", "rg", "aks");
    clusterCostApi.putBudget("sub", "rg", "aks", 125);

    expect(apiMocks.get).toHaveBeenCalledWith(
      "/cost?subscription_id=sub&resource_group=rg&cluster_name=aks",
    );
    expect(apiMocks.put).toHaveBeenCalledWith("/cost/budget", {
      subscription_id: "sub",
      resource_group: "rg",
      cluster_name: "aks",
      monthly_budget_usd: 125,
    });
  });

  it("keeps webhook endpoints relative to the shared /api prefix", () => {
    webhooksApi.get();
    webhooksApi.put({
      url: "https://example.invalid/hook",
      enabled: true,
      events: "all",
    });
    webhooksApi.test();

    expect(apiMocks.get).toHaveBeenCalledWith("/settings/webhooks");
    expect(apiMocks.put).toHaveBeenCalledWith("/settings/webhooks", {
      url: "https://example.invalid/hook",
      enabled: true,
      events: "all",
    });
    expect(apiMocks.post).toHaveBeenCalledWith("/settings/webhooks/test", {});
  });

  it("keeps BLAST template endpoints relative to the shared /api prefix", () => {
    const fields = {} as Parameters<typeof blastTemplatesApi.create>[1];
    blastTemplatesApi.list();
    blastTemplatesApi.create("preset", fields);
    blastTemplatesApi.update("template/id", { name: "renamed" });
    blastTemplatesApi.remove("template/id");

    expect(apiMocks.get).toHaveBeenCalledWith("/blast/templates");
    expect(apiMocks.post).toHaveBeenCalledWith("/blast/templates", {
      name: "preset",
      fields,
    });
    expect(apiMocks.put).toHaveBeenCalledWith("/blast/templates/template%2Fid", {
      name: "renamed",
    });
    expect(apiMocks.del).toHaveBeenCalledWith("/blast/templates/template%2Fid");
  });
});
