import { describe, expect, it } from "vitest";

import { tieCutoffNotice } from "./tieCutoffNotice";

describe("tieCutoffNotice", () => {
  it("prioritizes near-miss preservation when reservation and overflow coexist", () => {
    const notice = tieCutoffNotice({
      overflow_count: 7,
      diversity_reserved_count: 20,
      diversity_candidate_count: 80,
      diversity_reservation_mode: "proportional",
      max_target_seqs: 5000,
    });

    expect(notice.kind).toBe("diversity");
    expect(notice.message).toContain("reserved 20 slots from 80 lower-scoring candidates");
    expect(notice.message).toContain("strict cutoff excluded 7 tied top-score hits");
    expect(notice.message).toContain("20 additional tied hits were replaced");
    expect(notice.message).toContain("max_target_seqs=5000");
  });

  it("keeps the tied-class warning when strict selection reserved no slots", () => {
    const notice = tieCutoffNotice({
      overflow_count: 1,
      diversity_reserved_count: 0,
    });

    expect(notice.kind).toBe("overflow");
    expect(notice.message).toContain("1 hit with the same top score was not shown");
  });

  it("reports verified full-DB selection without suggesting heuristic drift", () => {
    const notice = tieCutoffNotice({
      overflow_count: 12,
      diversity_reserved_count: 0,
      max_target_seqs: 5000,
      selection_equivalence: "full_db_hitlist_exact",
      ranking_basis: "blast_evalue_raw_score_db_oid_desc",
    });

    expect(notice.kind).toBe("exact");
    expect(notice.message).toContain("Full-DB-exact merge");
    expect(notice.message).toContain("12 additional tied subjects");
  });
});