import { describe, expect, it } from "vitest";

import { capacityGateBandClass } from "@/api/blast";
import type { CapacityGateSnapshot } from "@/api/blast";

function makeSnapshot(over: Partial<CapacityGateSnapshot> = {}): CapacityGateSnapshot {
  return {
    enabled: true,
    pool: "blastpool",
    slots: { in_use: 0, max: 1 },
    cpu_request_pct: 10,
    memory_request_pct: 10,
    watermark_cpu_pct: 75,
    watermark_memory_pct: 75,
    pending_pods: 0,
    decision_preview: "admit",
    decision_reason: null,
    decision_retryable: false,
    predicted_demand: { cpu_m: 1000, mem_mib: 4096 },
    active_reservations: [],
    signals_degraded: false,
    signals_error: null,
    ...over,
  };
}

describe("capacityGateBandClass", () => {
  it("returns is-disabled when the gate is off", () => {
    expect(capacityGateBandClass(makeSnapshot({ enabled: false }))).toBe("is-disabled");
  });

  it("returns is-degraded when signals are missing", () => {
    expect(capacityGateBandClass(makeSnapshot({ signals_degraded: true }))).toBe(
      "is-degraded",
    );
  });

  it("returns is-danger when the gate is denying", () => {
    expect(
      capacityGateBandClass(
        makeSnapshot({ decision_preview: "deny", decision_reason: "cpu_watermark" }),
      ),
    ).toBe("is-danger");
  });

  it("returns is-warning when CPU crosses the watermark but gate still admits", () => {
    expect(
      capacityGateBandClass(
        makeSnapshot({ cpu_request_pct: 80, watermark_cpu_pct: 75 }),
      ),
    ).toBe("is-warning");
  });

  it("returns is-warning when memory crosses the watermark", () => {
    expect(
      capacityGateBandClass(
        makeSnapshot({ memory_request_pct: 90, watermark_memory_pct: 75 }),
      ),
    ).toBe("is-warning");
  });

  it("returns is-ok when everything is below watermark and the gate admits", () => {
    expect(capacityGateBandClass(makeSnapshot())).toBe("is-ok");
  });

  it("prefers disabled over signals_degraded so preview surfaces cleanly", () => {
    expect(
      capacityGateBandClass(makeSnapshot({ enabled: false, signals_degraded: true })),
    ).toBe("is-disabled");
  });
});
