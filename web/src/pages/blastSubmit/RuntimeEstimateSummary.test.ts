import { describe, expect, it } from "vitest";

import {
  runtimeComputeBasisLabel,
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

  it("does not round a sub-hour estimate into the next hour", () => {
    expect(runtimeEstimateLabel(3_599, null, null)).toBe("59m");
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

describe("runtimeComputeBasisLabel", () => {
  it("makes the workload shape and pricing assumption explicit", () => {
    expect(
      runtimeComputeBasisLabel(
        {
          subscription_id: "sub",
          resource_group: "rg",
          cluster_name: "aks",
          program: "blastn",
          database: "core_nt",
          query_letters: 100,
          database_letters: 1_000,
          node_count: 3,
          node_sku: "Standard_E32s_v5",
        },
        { source: "static", priced_as_of: "2026-06" },
      ),
    ).toBe("Standard_E32s_v5 × 3 nodes · static on-demand estimate (2026-06)");
  });

  it("distinguishes live on-demand pricing", () => {
    expect(
      runtimeComputeBasisLabel(
        {
          subscription_id: "sub",
          resource_group: "rg",
          cluster_name: "aks",
          program: "blastn",
          database: "core_nt",
          query_letters: 100,
          database_letters: 1_000,
          node_count: 1,
          node_sku: "Standard_E16s_v5",
        },
        { source: "live", priced_as_of: "2026-09-19" },
      ),
    ).toBe("Standard_E16s_v5 × 1 node · live on-demand rate");
  });
});