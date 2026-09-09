import { describe, expect, it } from "vitest";

import {
  runtimeEstimateLabel,
  runtimeEstimateStatusLabel,
} from "./RuntimeEstimateSummary";

describe("runtimeEstimateLabel", () => {
  it("shows a median and interquartile range", () => {
    expect(runtimeEstimateLabel(3600, 1800, 5400)).toBe("1h 0m (30m–1h 30m)");
  });

  it("degrades when evidence is absent", () => {
    expect(runtimeEstimateLabel(null, null, null)).toBe("—");
  });
});

describe("runtimeEstimateStatusLabel", () => {
  it("hides stale data while inputs debounce", () => {
    expect(
      runtimeEstimateStatusLabel({
        available: true,
        estimateSeconds: 60,
        isLoading: false,
        isError: false,
        inputPending: true,
      }),
    ).toBe("Calculating…");
  });

  it("distinguishes failures from insufficient evidence", () => {
    expect(
      runtimeEstimateStatusLabel({
        reason: "estimate_unavailable",
        isLoading: false,
        isError: false,
        inputPending: false,
      }),
    ).toBe("Estimate unavailable");
    expect(
      runtimeEstimateStatusLabel({
        reason: "insufficient_samples",
        sampleCount: 2,
        requiredSamples: 3,
        isLoading: false,
        isError: false,
        inputPending: false,
      }),
    ).toBe("Collecting baseline (2/3)");
  });
});