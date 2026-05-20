import { describe, expect, it } from "vitest";

import type { AksClusterSummary } from "@/api/endpoints";
import {
  INITIAL,
  PROGRAMS,
  buildCommandString,
  type FormState,
} from "@/pages/blastSubmitModel";
import { buildSubmitRequest } from "@/pages/blastSubmit/useSubmitMutation";

const cluster: AksClusterSummary = {
  name: "aks-elb",
  resource_group: "rg-elb",
  region: "koreacentral",
  k8s_version: "1.34",
  provisioning_state: "Succeeded",
  power_state: "Running",
  node_count: 3,
  node_sku: "Standard_E16s_v5",
  kubelet_object_id: null,
  agent_pools: [
    {
      name: "blastpool",
      vm_size: "Standard_E16s_v5",
      count: 3,
      min_count: null,
      max_count: null,
      os_type: "Linux",
      mode: "User",
      power_state: "Running",
      enable_auto_scaling: false,
    },
  ],
};

function makeForm(overrides: Partial<FormState> = {}): FormState {
  return {
    ...INITIAL,
    program: "blastn",
    db: "blast-db/core_nt/core_nt",
    query_data: ">q1\nATGC",
    selectedCluster: cluster.name,
    ...overrides,
  };
}

function makeRequest(form: FormState) {
  return buildSubmitRequest({
    form,
    selectedCluster: cluster,
    subId: "sub-1",
    workloadRg: "rg-elb",
    storageAccount: "stelb",
    acrRg: "rg-elbacr",
    acrName: "acrelb",
    region: "koreacentral",
  });
}

describe("blast submit taxonomy filter", () => {
  it("adds an inclusive taxid filter to the submit payload", () => {
    const request = makeRequest(makeForm({ taxid: "9606", is_inclusive: true }));

    expect(request.taxid).toBe(9606);
    expect(request.is_inclusive).toBe(true);
  });

  it("adds an exclusive taxid filter to the submit payload", () => {
    const request = makeRequest(makeForm({ taxid: "562", is_inclusive: false }));

    expect(request.taxid).toBe(562);
    expect(request.is_inclusive).toBe(false);
  });

  it("omits taxonomy fields when no taxid is selected", () => {
    const request = makeRequest(makeForm({ taxid: "", is_inclusive: false }));

    expect(request.taxid).toBeUndefined();
    expect(request.is_inclusive).toBeUndefined();
  });

  it("rejects invalid taxonomy ids before building a submit payload", () => {
    expect(() => makeRequest(makeForm({ taxid: "abc" }))).toThrow(
      "Taxonomy taxid must be a positive integer",
    );
    expect(() => makeRequest(makeForm({ taxid: "0" }))).toThrow(
      "Taxonomy taxid must be a positive integer",
    );
  });

  it("rejects conflicting taxonomy flags in additional options", () => {
    expect(() =>
      makeRequest(makeForm({ taxid: "9606", additional_options: "-taxids 562" })),
    ).toThrow("Remove -taxids or -negative_taxids");
    expect(() =>
      makeRequest(
        makeForm({ taxid: "9606", additional_options: "-negative_taxids 562" }),
      ),
    ).toThrow("Remove -taxids or -negative_taxids");
  });

  it("adds the verified DB search space to the submit payload", () => {
    const request = buildSubmitRequest({
      form: makeForm(),
      selectedCluster: cluster,
      subId: "sub-1",
      workloadRg: "rg-elb",
      storageAccount: "stelb",
      acrRg: "rg-elbacr",
      acrName: "acrelb",
      region: "koreacentral",
      dbEffectiveSearchSpace: 32156241807668,
    });

    expect(request.db_effective_search_space).toBe(32156241807668);
  });

  it("forces stale sharding form state off when no prepared shard layout is available", () => {
    const request = buildSubmitRequest({
      form: makeForm({ sharding_mode: "precise", db_auto_partition: true }),
      selectedCluster: cluster,
      subId: "sub-1",
      workloadRg: "rg-elb",
      storageAccount: "stelb",
      acrRg: "rg-elbacr",
      acrName: "acrelb",
      region: "koreacentral",
      dbShardSets: [],
    });

    expect(request.sharding_mode).toBe("off");
    expect(request.db_auto_partition).toBe(false);
    expect(request.shard_sets).toBeUndefined();
    expect(request.disable_sharding).toBe(true);
  });

  it("treats single-part non-core-nt shard metadata as unsharded", () => {
    const request = buildSubmitRequest({
      form: makeForm({
        db: "blast-db/18S_fungal_sequences/18S_fungal_sequences",
        sharding_mode: "precise",
        db_auto_partition: true,
      }),
      selectedCluster: cluster,
      subId: "sub-1",
      workloadRg: "rg-elb",
      storageAccount: "stelb",
      acrRg: "rg-elbacr",
      acrName: "acrelb",
      region: "koreacentral",
      dbShardSets: [1],
    });

    expect(request.sharding_mode).toBe("off");
    expect(request.db_auto_partition).toBe(false);
    expect(request.shard_sets).toBeUndefined();
    expect(request.disable_sharding).toBe(true);
  });

  it("keeps sharding enabled when a prepared shard layout is available", () => {
    const request = buildSubmitRequest({
      form: makeForm({ sharding_mode: "precise", db_auto_partition: true }),
      selectedCluster: cluster,
      subId: "sub-1",
      workloadRg: "rg-elb",
      storageAccount: "stelb",
      acrRg: "rg-elbacr",
      acrName: "acrelb",
      region: "koreacentral",
      dbShardSets: [1, 2, 4, 10],
    });

    expect(request.sharding_mode).toBe("precise");
    expect(request.db_auto_partition).toBe(true);
    expect(request.shard_sets).toEqual([1, 2, 4, 10]);
    expect(request.disable_sharding).toBe(false);
    expect(request.use_db_order_oracle).toBe(true);
  });

  it("renders inclusive taxonomy filters in the command preview", () => {
    const command = buildCommandString(
      makeForm({ taxid: "9606", is_inclusive: true }),
      PROGRAMS[0],
    );

    expect(command).toContain("-taxids 9606");
  });

  it("renders exclusive taxonomy filters in the command preview", () => {
    const command = buildCommandString(
      makeForm({ taxid: "562", is_inclusive: false }),
      PROGRAMS[0],
    );

    expect(command).toContain("-negative_taxids 562");
  });

  it("renders the verified DB search space in the command preview", () => {
    const command = buildCommandString(makeForm(), PROGRAMS[0], {
      effectiveSearchSpace: 32156241807668,
    });

    expect(command).toContain("-searchsp 32156241807668");
  });

  it("does not duplicate an explicit search space override in the command preview", () => {
    const command = buildCommandString(
      makeForm({ additional_options: "-searchsp 42" }),
      PROGRAMS[0],
      { effectiveSearchSpace: 32156241807668 },
    );

    expect(command.match(/-searchsp/g)).toHaveLength(1);
    expect(command).toContain("-searchsp 42");
  });

  it("maps NCBI-style masking and culling controls into the submit options", () => {
    const request = makeRequest(
      makeForm({
        max_matches_in_query_range: "2",
        mask_lookup_table_only: true,
        mask_lowercase: true,
        species_repeat_filter: true,
        repeat_filter_taxid: "9606",
      }),
    );

    expect(request.additional_options).toContain("-culling_limit 2");
    expect(request.additional_options).toContain("-soft_masking true");
    expect(request.additional_options).toContain("-lcase_masking");
    expect(request.additional_options).toContain("-window_masker_taxid 9606");
  });

  it("uses Web BLAST-compatible hard masking by default", () => {
    const form = makeForm();
    const request = makeRequest(form);
    const command = buildCommandString(form, PROGRAMS[0]);

    expect(form.mask_lookup_table_only).toBe(false);
    expect(request.additional_options).toContain("-dust yes");
    expect(request.additional_options).toContain("-soft_masking false");
    expect(request.additional_options).not.toContain("-soft_masking true");
    expect(command).toContain("-soft_masking false");
  });

  it("uses blastn-short for short blastn queries when automatic adjustment is enabled", () => {
    const request = makeRequest(makeForm({ query_data: ">primer\nATGCATGCATGC" }));
    const command = buildCommandString(
      makeForm({ query_data: ">primer\nATGCATGCATGC" }),
      PROGRAMS[0],
    );

    expect(request.additional_options).toContain("-task blastn-short");
    expect(command).toContain("-task blastn-short");
  });
});
