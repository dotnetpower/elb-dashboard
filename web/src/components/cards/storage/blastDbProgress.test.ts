import { describe, expect, it } from "vitest";

import {
  formatDuration,
  formatEta,
} from "@/components/cards/storage/blastDbProgress";

describe("formatDuration", () => {
  it("renders seconds under a minute", () => {
    expect(formatDuration(0)).toBe("0s");
    expect(formatDuration(45)).toBe("45s");
    expect(formatDuration(59)).toBe("59s");
  });

  it("renders whole minutes under an hour", () => {
    expect(formatDuration(60)).toBe("1m");
    expect(formatDuration(150)).toBe("3m");
    expect(formatDuration(3540)).toBe("59m");
  });

  it("renders hours and minutes", () => {
    expect(formatDuration(3600)).toBe("1h");
    expect(formatDuration(3900)).toBe("1h 5m");
    expect(formatDuration(7200)).toBe("2h");
  });

  it("clamps negatives to zero", () => {
    expect(formatDuration(-10)).toBe("0s");
  });
});

describe("formatEta", () => {
  it("returns empty when there is no total", () => {
    expect(formatEta(120, 10, 0)).toBe("");
  });

  it("returns empty once every file is copied", () => {
    expect(formatEta(120, 800, 800)).toBe("");
    expect(formatEta(120, 801, 800)).toBe("");
  });

  it("reports estimating before throughput stabilises", () => {
    expect(formatEta(120, 0, 800)).toBe("estimating…");
    expect(formatEta(3, 10, 800)).toBe("estimating…");
  });

  it("projects remaining time from observed throughput", () => {
    // 10 files in 100 s → 10 s/file; 790 remaining → ~7900 s → 2h 12m.
    expect(formatEta(100, 10, 800)).toBe("~2h 12m left");
    // Half done in 600 s → ~600 s remaining → 10m.
    expect(formatEta(600, 400, 800)).toBe("~10m left");
  });
});
