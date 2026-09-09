import { describe, expect, it } from "vitest";

import { comparisonSummaryText } from "./ResultComparisonPanel";

describe("comparisonSummaryText", () => {
  it("summarizes the three user-visible change classes", () => {
    expect(
      comparisonSummaryText({
        before_hits: 20,
        after_hits: 22,
        common: 18,
        unchanged: 15,
        changed: 3,
        added: 4,
        removed: 2,
        jaccard_percent: 75,
      }),
    ).toBe("4 added · 2 removed · 3 changed");
  });
});