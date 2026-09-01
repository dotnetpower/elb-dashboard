import { describe, expect, it } from "vitest";

import { tieCutoffNotice } from "./tieCutoffNotice";

describe("tieCutoffNotice", () => {
  it("prioritizes near-miss preservation when reservation and overflow coexist", () => {
    const notice = tieCutoffNotice({
      overflow_count: 7,
      diversity_reserved_count: 1,
      max_target_seqs: 100,
    });

    expect(notice.kind).toBe("diversity");
    expect(notice.message).toContain("reserved 1 slot");
    expect(notice.message).toContain("strict cutoff excluded 7 tied top-score hits");
    expect(notice.message).toContain("1 additional tied hit was replaced");
    expect(notice.message).toContain("max_target_seqs=100");
  });

  it("keeps the tied-class warning when strict selection reserved no slots", () => {
    const notice = tieCutoffNotice({
      overflow_count: 1,
      diversity_reserved_count: 0,
    });

    expect(notice.kind).toBe("overflow");
    expect(notice.message).toContain("1 hit with the same top score was not shown");
  });
});