import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  listDatabases: vi.fn(),
}));

vi.mock("@/api/endpoints", () => ({
  blastApi: {
    listDatabases: mocks.listDatabases,
  },
}));

import {
  BLAST_DATABASES_PREFETCH_STALE_MS,
  prefetchBlastDatabasesQuery,
} from "@/hooks/usePrefetchBlastDatabases";

interface Captured {
  queryKey: unknown[];
  staleTime?: number;
}

function makeClient(captured: Captured[]) {
  return {
    async prefetchQuery(options: {
      queryKey: unknown[];
      queryFn: () => unknown;
      staleTime?: number;
    }) {
      captured.push({ queryKey: options.queryKey, staleTime: options.staleTime });
      return options.queryFn();
    },
  };
}

describe("prefetchBlastDatabasesQuery", () => {
  beforeEach(() => {
    mocks.listDatabases.mockReset();
    mocks.listDatabases.mockResolvedValue({ databases: [] });
  });

  it("prefetches with a topology-free key matching the page's first render", async () => {
    const captured: Captured[] = [];
    await prefetchBlastDatabasesQuery(makeClient(captured), {
      subscriptionId: "sub-1",
      storageAccount: "stacct",
      workloadResourceGroup: "rg-work",
    });

    expect(captured).toHaveLength(1);
    expect(captured[0].queryKey).toEqual([
      "blast-databases",
      "sub-1",
      "stacct",
      0,
      "",
    ]);
    expect(captured[0].staleTime).toBe(BLAST_DATABASES_PREFETCH_STALE_MS);
    expect(mocks.listDatabases).toHaveBeenCalledWith("sub-1", "stacct", "rg-work");
  });

  it.each([
    ["", "stacct", "rg-work"],
    ["sub-1", "", "rg-work"],
    ["sub-1", "stacct", ""],
  ])(
    "skips the prefetch when a required field is missing (%s/%s/%s)",
    async (subscriptionId, storageAccount, workloadResourceGroup) => {
      const captured: Captured[] = [];
      await prefetchBlastDatabasesQuery(makeClient(captured), {
        subscriptionId,
        storageAccount,
        workloadResourceGroup,
      });
      expect(captured).toHaveLength(0);
      expect(mocks.listDatabases).not.toHaveBeenCalled();
    },
  );
});
