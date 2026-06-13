import { QueryClient } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  listResourceGroups: vi.fn(),
}));

vi.mock("@/api/endpoints", () => ({
  armProxyApi: {
    listResourceGroups: mocks.listResourceGroups,
  },
}));

import {
  RESOURCE_GROUPS_STALE_MS,
  fetchResourceGroups,
  resourceGroupsQueryKey,
} from "@/api/resourceGroups";

function makeClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
}

describe("fetchResourceGroups", () => {
  beforeEach(() => {
    mocks.listResourceGroups.mockReset();
  });

  it("uses the canonical resource-groups query key", () => {
    expect(resourceGroupsQueryKey("sub-1")).toEqual([
      "arm",
      "resource-groups",
      "sub-1",
    ]);
  });

  it("dedupes concurrent callers into a single upstream ARM call", async () => {
    const rows = [{ name: "rg-elb-dashboard", location: "koreacentral" }];
    mocks.listResourceGroups.mockResolvedValue(rows);
    const qc = makeClient();

    const [a, b] = await Promise.all([
      fetchResourceGroups(qc, "sub-1"),
      fetchResourceGroups(qc, "sub-1"),
    ]);

    expect(a).toEqual(rows);
    expect(b).toEqual(rows);
    // Two readers, one in-flight fetch under the shared key.
    expect(mocks.listResourceGroups).toHaveBeenCalledTimes(1);
  });

  it("serves a second call from cache within the stale window", async () => {
    const rows = [{ name: "rg-a", location: "koreacentral" }];
    mocks.listResourceGroups.mockResolvedValue(rows);
    const qc = makeClient();

    await fetchResourceGroups(qc, "sub-1");
    await fetchResourceGroups(qc, "sub-1");

    expect(mocks.listResourceGroups).toHaveBeenCalledTimes(1);
    // Sanity: the shared stale window is non-trivial so first-paint readers
    // (picker + ClusterCard) actually collapse onto one fetch.
    expect(RESOURCE_GROUPS_STALE_MS).toBeGreaterThanOrEqual(30_000);
  });

  it("issues separate fetches for different subscriptions", async () => {
    mocks.listResourceGroups.mockResolvedValue([]);
    const qc = makeClient();

    await fetchResourceGroups(qc, "sub-1");
    await fetchResourceGroups(qc, "sub-2");

    expect(mocks.listResourceGroups).toHaveBeenCalledTimes(2);
  });
});
