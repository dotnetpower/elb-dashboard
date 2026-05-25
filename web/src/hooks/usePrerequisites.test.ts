import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  aks: vi.fn(),
  loadSavedConfig: vi.fn(),
  useQuery: vi.fn(),
}));

vi.mock("@tanstack/react-query", () => ({
  useQuery: mocks.useQuery,
}));

vi.mock("@/api/endpoints", () => ({
  monitoringApi: {
    aks: mocks.aks,
  },
}));

vi.mock("@/components/SetupWizard", () => ({
  loadSavedConfig: mocks.loadSavedConfig,
}));

import { useClusterReadiness } from "@/hooks/usePrerequisites";

interface QueryOptions {
  queryKey: unknown[];
  queryFn: () => unknown;
  enabled?: boolean;
}

describe("useClusterReadiness", () => {
  let capturedOptions: QueryOptions | null = null;

  beforeEach(() => {
    capturedOptions = null;
    mocks.aks.mockReset();
    mocks.loadSavedConfig.mockReset();
    mocks.useQuery.mockReset();
    mocks.loadSavedConfig.mockReturnValue({
      subscriptionId: "sub-123",
      workloadResourceGroup: "rg-anchor",
    });
    mocks.useQuery.mockImplementation((options: QueryOptions) => {
      capturedOptions = options;
      return {
        data: {
          clusters: [
            {
              name: "elb-cluster-01",
              resource_group: "rg-elb-cluster",
              provisioning_state: "Succeeded",
              power_state: "Running",
            },
          ],
        },
        isError: false,
        isLoading: false,
      };
    });
  });

  it("uses subscription-wide AKS discovery instead of the workspace anchor RG", () => {
    const readiness = useClusterReadiness();

    expect(readiness.hasAnyCluster).toBe(true);
    expect(readiness.hasRunningCluster).toBe(true);
    expect(capturedOptions?.queryKey).toEqual(["aks", "sub-123", "sub"]);

    capturedOptions?.queryFn();
    expect(mocks.aks).toHaveBeenCalledWith("sub-123");
  });
});