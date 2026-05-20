import { describe, expect, it } from "vitest";

import type { AksClusterSummary, BlastDatabase } from "@/api/endpoints";
import {
  deriveShardingAvailability,
  reconcileShardingSelection,
} from "@/pages/blastSubmit/shardingAvailability";
import { INITIAL } from "@/pages/blastSubmitModel";

const cluster: AksClusterSummary = {
  name: "elb-cluster",
  resource_group: "rg-elb-01",
  region: "koreacentral",
  k8s_version: "1.34",
  provisioning_state: "Succeeded",
  power_state: "Running",
  node_count: 10,
  node_sku: "Standard_E16s_v5",
  kubelet_object_id: null,
  agent_pools: [
    {
      name: "blastpool",
      vm_size: "Standard_E16s_v5",
      count: 10,
      min_count: null,
      max_count: null,
      os_type: "Linux",
      mode: "User",
      power_state: "Running",
      enable_auto_scaling: false,
    },
  ],
};

const database: BlastDatabase = {
  name: "core_nt",
  container: "blast-db",
  total_bytes: 250 * 1024 ** 3,
  web_blast_searchsp: 32_156_241_807_668,
  sharded: true,
  shard_sets: [1, 2, 3, 4, 5, 6, 8, 10],
};

describe("deriveShardingAvailability", () => {
  it("enables precise sharding for a warm prepared DB that fits the cluster", () => {
    const availability = deriveShardingAvailability({
      cluster,
      database,
      isDbAlreadyWarm: true,
      outfmt: 5,
    });

    expect(availability.options.off.enabled).toBe(true);
    expect(availability.preferredMode).toBe("precise");
    expect(availability.options.precise.enabled).toBe(true);
    expect(availability.options.precise.label).toBe("Web-equivalent shard");
    expect(availability.options.approximate.enabled).toBe(true);
    expect(availability.capacityPlan?.pickedN).toBe(10);
  });

  it("keeps precise mode from claiming Web equivalence without evidence", () => {
    const availability = deriveShardingAvailability({
      cluster,
      database: { ...database, web_blast_searchsp: undefined },
      isDbAlreadyWarm: true,
      outfmt: 5,
    });

    expect(availability.preferredMode).toBe("approximate");
    expect(availability.options.approximate.enabled).toBe(true);
    expect(availability.options.precise.enabled).toBe(false);
    expect(availability.options.precise.label).toBe("Precise shard");
    expect(availability.options.precise.reason).toContain(
      "Verified Web BLAST search-space evidence",
    );
  });

  it("disables sharded modes when the DB is not warm on the selected cluster", () => {
    const availability = deriveShardingAvailability({
      cluster,
      database,
      isDbAlreadyWarm: false,
      outfmt: 5,
    });

    expect(availability.options.off.enabled).toBe(true);
    expect(availability.preferredMode).toBe("off");
    expect(availability.options.precise.enabled).toBe(false);
    expect(availability.options.precise.reason).toContain("Warm this database");
  });

  it("keeps baseline mode available for warm DBs without prepared shard layouts", () => {
    const availability = deriveShardingAvailability({
      cluster,
      database: {
        ...database,
        name: "18S_fungal_sequences",
        sharded: false,
        shard_sets: [],
      },
      isDbAlreadyWarm: true,
      outfmt: 5,
    });

    expect(availability.preferredMode).toBe("off");
    expect(availability.options.off.enabled).toBe(true);
    expect(availability.options.approximate.enabled).toBe(false);
    expect(availability.options.precise.enabled).toBe(false);
    expect(availability.options.precise.reason).toContain("Prepared shard layouts");
  });

  it("does not treat single-part non-core-nt shard metadata as prepared sharding", () => {
    const availability = deriveShardingAvailability({
      cluster,
      database: {
        ...database,
        name: "18S_fungal_sequences",
        sharded: true,
        shard_sets: [1],
      },
      isDbAlreadyWarm: true,
      outfmt: 5,
    });

    expect(availability.preferredMode).toBe("off");
    expect(availability.options.off.enabled).toBe(true);
    expect(availability.options.approximate.enabled).toBe(false);
    expect(availability.options.precise.enabled).toBe(false);
    expect(availability.capacityPlan).toBeNull();
  });

  it("disables sharded modes when capacity requires more shards than nodes", () => {
    const availability = deriveShardingAvailability({
      cluster,
      database: { ...database, total_bytes: 800 * 1024 ** 3 },
      isDbAlreadyWarm: true,
      outfmt: 5,
    });

    expect(availability.options.approximate.enabled).toBe(false);
    expect(availability.options.approximate.reason).toContain("needs at least");
  });

  it("requires a merge-compatible output format", () => {
    const availability = deriveShardingAvailability({
      cluster,
      database,
      isDbAlreadyWarm: true,
      outfmt: 7,
    });

    expect(availability.options.precise.enabled).toBe(false);
    expect(availability.options.precise.reason).toContain("output format 5 or 6");
  });
});

describe("reconcileShardingSelection", () => {
  it("promotes the default warmed profile to the preferred sharded mode", () => {
    const availability = deriveShardingAvailability({
      cluster,
      database,
      isDbAlreadyWarm: true,
      outfmt: 5,
    });

    const result = reconcileShardingSelection({
      form: {
        ...INITIAL,
        enable_warmup: true,
        sharding_mode: "off",
        db_auto_partition: false,
        disable_sharding: false,
      },
      availability,
      isDbAlreadyWarm: true,
    });

    expect(result.enable_warmup).toBe(true);
    expect(result.sharding_mode).toBe("precise");
    expect(result.db_auto_partition).toBe(true);
    expect(result.disable_sharding).toBe(false);
  });

  it("preserves an explicit sharding opt-out", () => {
    const availability = deriveShardingAvailability({
      cluster,
      database,
      isDbAlreadyWarm: true,
      outfmt: 5,
    });
    const form = {
      ...INITIAL,
      enable_warmup: true,
      sharding_mode: "off" as const,
      db_auto_partition: false,
      disable_sharding: true,
    };

    const result = reconcileShardingSelection({
      form,
      availability,
      isDbAlreadyWarm: true,
    });

    expect(result).toBe(form);
  });

  it("falls back when a selected sharded mode becomes unavailable", () => {
    const availability = deriveShardingAvailability({
      cluster,
      database,
      isDbAlreadyWarm: false,
      outfmt: 5,
    });

    const result = reconcileShardingSelection({
      form: {
        ...INITIAL,
        enable_warmup: true,
        sharding_mode: "precise",
        db_auto_partition: true,
        disable_sharding: false,
      },
      availability,
      isDbAlreadyWarm: false,
    });

    expect(result.sharding_mode).toBe("off");
    expect(result.db_auto_partition).toBe(false);
    expect(result.disable_sharding).toBe(false);
  });
});
