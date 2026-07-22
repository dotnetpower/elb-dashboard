import { describe, expect, it } from "vitest";

import type { SidecarsSnapshot } from "@/hooks/useSidecarMetrics";

import { formatMemBytesCompact, memLabel, staleSnapshot } from "./helpers";

describe("formatMemBytesCompact", () => {
  it("renders bytes/KiB/MiB/GiB with compact units", () => {
    expect(formatMemBytesCompact(512)).toBe("512B");
    expect(formatMemBytesCompact(2048)).toBe("2K");
    expect(formatMemBytesCompact(134217728)).toBe("128M");
    expect(formatMemBytesCompact(1610612736)).toBe("1.5G");
  });
});

describe("memLabel", () => {
  it("prefers a percentage when mem_pct is present", () => {
    expect(memLabel(42, 134217728)).toBe("42%");
    expect(memLabel(0, 134217728)).toBe("0%");
  });

  it("falls back to absolute bytes when mem_pct is null (no cgroup limit)", () => {
    expect(memLabel(null, 134217728)).toBe("128M");
    expect(memLabel(undefined, 2048)).toBe("2K");
  });

  it("renders an em dash when neither value is available", () => {
    expect(memLabel(null, undefined)).toBe("—");
    expect(memLabel(undefined, undefined)).toBe("—");
  });
});

describe("staleSnapshot", () => {
  const baseSnapshot: SidecarsSnapshot = {
    ts: 1_700_000_000,
    revision: "rev-1",
    sidecars: {
      api: {
        name: "api",
        health: "ok",
        ts: 1_700_000_000,
        cpu_pct: 12,
        mem_bytes: 134217728,
        mem_pct: 55,
      },
    },
  };

  it("clears cpu, mem_pct AND mem_bytes so a stale tile shows mem —", () => {
    const stale = staleSnapshot(baseSnapshot);
    const api = stale?.sidecars.api;
    expect(api?.health).toBe("degraded");
    expect(api?.cpu_pct).toBeUndefined();
    expect(api?.mem_pct).toBeUndefined();
    expect(api?.mem_bytes).toBeUndefined();
    expect(memLabel(api?.mem_pct, api?.mem_bytes)).toBe("—");
  });

  it("returns undefined unchanged", () => {
    expect(staleSnapshot(undefined)).toBeUndefined();
  });
});
