import { describe, expect, it } from "vitest";

import {
  analyticsFilterQueryKey,
  type BlastAnalyticsFilters,
} from "./useBlastAnalyticsState";
import { isPartialResult, isResultFilesUnavailable } from "./helpers";

const baseFilters: BlastAnalyticsFilters = {
  queryFilter: "q1",
  subjectFilter: "NC_001",
  organismFilter: "Monkeypox virus",
  minIdentity: 90,
  maxIdentity: 100,
  minQueryCover: 70,
  maxQueryCover: 100,
  maxEvalue: 0.001,
  sortBy: "bitscore",
  sortDir: "desc",
  pageSize: 100,
};

describe("BLAST analytics state helpers", () => {
  it("uses primitive filter values for the alignments query key", () => {
    const sameValuesDifferentObject = { ...baseFilters };

    expect(analyticsFilterQueryKey(baseFilters)).toEqual(
      analyticsFilterQueryKey(sameValuesDifferentObject),
    );
    expect(analyticsFilterQueryKey(baseFilters)).not.toContain(baseFilters);
  });

  it("treats unavailable result files as partial and explainable", () => {
    expect(isPartialResult({ degraded: true, degraded_reason: "no_result_files" })).toBe(
      true,
    );
    expect(isResultFilesUnavailable({ degraded_reason: "no_result_files" })).toBe(true);
    expect(isResultFilesUnavailable({ degraded_reason: "storage_unreachable" })).toBe(
      true,
    );
    expect(isResultFilesUnavailable({ degraded_reason: "aggregation_failed" })).toBe(
      false,
    );
  });
});
