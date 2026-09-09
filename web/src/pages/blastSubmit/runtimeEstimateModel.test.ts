import { describe, expect, it } from "vitest";

import {
  buildRuntimeEstimateInput,
  fastaLetterCount,
} from "./runtimeEstimateModel";

describe("runtime estimate input", () => {
  it("counts FASTA letters across records", () => {
    expect(fastaLetterCount(">a\nACGT\n>b\nAAA")).toBe(7);
  });

  it("uses the workload pool rather than the system pool", () => {
    const result = buildRuntimeEstimateInput({
      subscriptionId: "sub",
      program: "blastn",
      database: { name: "core_nt", total_letters: 1_000_000 },
      queryData: ">q\nACGT",
      cluster: {
        name: "cluster",
        resource_group: "rg",
        region: "koreacentral",
        k8s_version: null,
        provisioning_state: "Succeeded",
        power_state: "Running",
        node_count: 3,
        node_sku: "system-sku",
        kubelet_object_id: null,
        agent_pools: [
          {
            name: "systempool",
            mode: "System",
            vm_size: "system-sku",
            count: 1,
            min_count: 1,
            max_count: 1,
            os_type: "Linux",
            power_state: "Running",
            enable_auto_scaling: false,
          },
          {
            name: "blastpool",
            mode: "User",
            vm_size: "Standard_E16s_v5",
            count: 2,
            min_count: 0,
            max_count: 10,
            os_type: "Linux",
            power_state: "Running",
            enable_auto_scaling: true,
          },
        ],
      },
    });

    expect(result?.node_count).toBe(2);
    expect(result?.node_sku).toBe("Standard_E16s_v5");
    expect(result?.query_letters).toBe(4);
  });

  it("does not estimate a zero-node stopped workload pool", () => {
    const result = buildRuntimeEstimateInput({
      subscriptionId: "sub",
      program: "blastn",
      database: { name: "core_nt", total_letters: 1_000_000 },
      queryData: ">q\nACGT",
      cluster: {
        name: "cluster",
        resource_group: "rg",
        region: "koreacentral",
        k8s_version: null,
        provisioning_state: "Succeeded",
        power_state: "Stopped",
        node_count: 1,
        node_sku: "system-sku",
        kubelet_object_id: null,
        agent_pools: [
          {
            name: "blastpool",
            mode: "User",
            vm_size: "Standard_E16s_v5",
            count: 0,
            min_count: 0,
            max_count: 10,
            os_type: "Linux",
            power_state: "Stopped",
            enable_auto_scaling: true,
          },
        ],
      },
    });

    expect(result).toBeNull();
  });
});