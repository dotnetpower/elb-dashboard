import { describe, expect, it } from "vitest";

import { runtimeEstimateLabel } from "./RuntimeEstimateSummary";

describe("runtimeEstimateLabel", () => {
  it("shows a median and interquartile range", () => {
    expect(runtimeEstimateLabel(3600, 1800, 5400)).toBe("1h 0m (30m–1h 30m)");
  });

  it("degrades when evidence is absent", () => {
    expect(runtimeEstimateLabel(null, null, null)).toBe("—");
  });
});