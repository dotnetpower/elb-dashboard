import { describe, expect, it } from "vitest";

import {
  runtimeShardingDisplay,
  runtimeWarmupDisplay,
} from "@/pages/blastSubmit/runtimeSummaryDisplay";

describe("runtimeWarmupDisplay", () => {
  it("reports an already-warm database as ready even when enable_warmup is still off", () => {
    // Reproduces the reported bug: core_nt is warm on the cluster but the
    // reconcile effect has not yet flipped form.enable_warmup, so the raw form
    // value is false. The summary must still say the cache is ready.
    expect(
      runtimeWarmupDisplay({ isDbAlreadyWarm: true, enableWarmup: false }),
    ).toBe("warm cache ready");
  });

  it("reports enabled when warmup is requested but the DB is not yet warm", () => {
    expect(
      runtimeWarmupDisplay({ isDbAlreadyWarm: false, enableWarmup: true }),
    ).toBe("enabled");
  });

  it("reports off only when neither warm nor requested", () => {
    expect(
      runtimeWarmupDisplay({ isDbAlreadyWarm: false, enableWarmup: false }),
    ).toBe("off");
  });
});

describe("runtimeShardingDisplay", () => {
  it("prefers the effective sharding mode over the raw form value", () => {
    // The submit payload uses effectiveShardingMode; the summary must match so
    // it does not show "off" while a precise sharded run is actually queued.
    expect(
      runtimeShardingDisplay({
        effectiveShardingMode: "precise",
        formShardingMode: "off",
      }),
    ).toBe("precise");
  });

  it("falls back to the form value when no effective mode is provided", () => {
    expect(
      runtimeShardingDisplay({
        effectiveShardingMode: undefined,
        formShardingMode: "approximate",
      }),
    ).toBe("approximate");
  });
});
